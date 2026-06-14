from __future__ import annotations

import json
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from analyzer.ruby.parse_ruby import (
    RubyAnalysis,
    RubyParserUnavailable,
    RubyParserError,
    parse_ruby_analysis,
)
from analyzer.ruby.ruby_folder_index import build_folder_index, save_folder_index
from analyzer.ruby.ruby_build_artifacts import (
    build_nodefacts_from_folder_index,
    build_edges_from_folder_index,
)

try:
    from analyzer_store.types import FOLDER_INDEX_SCHEMA
except Exception:
    FOLDER_INDEX_SCHEMA = "folder-index-1.1.0"

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
    print(f"[RUBY] {name} {parts}".rstrip())


def _json_safe(obj: Any) -> Any:
    """
    Convert Ruby parser/model objects into JSON-safe structures.

    This protects artifact writes from nested parser objects such as RubyLoc,
    declaration/method/reference model instances, Path objects, dataclasses, sets,
    and other lightweight attribute objects.
    """
    if obj is None:
        return None

    if isinstance(obj, (str, int, float, bool)):
        return obj

    if isinstance(obj, Path):
        return str(obj)

    if is_dataclass(obj):
        try:
            return _json_safe(asdict(obj))
        except Exception:
            pass

    if isinstance(obj, dict):
        return {
            str(_json_safe(k)): _json_safe(v)
            for k, v in obj.items()
        }

    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_json_safe(v) for v in obj]

    d = getattr(obj, "__dict__", None)
    if isinstance(d, dict):
        return {
            str(k): _json_safe(v)
            for k, v in d.items()
            if not str(k).startswith("_")
        }

    return str(obj)


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_json_safe(obj), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _safe_len(v: Any) -> int:
    try:
        return len(v) if v is not None else 0
    except Exception:
        return 0


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None:
            return default
        return int(v)
    except Exception:
        return default


def _nested_len(obj: Any, key: str) -> int:
    return _safe_len(obj.get(key)) if isinstance(obj, dict) else 0


def _iter_file_raw_records(analysis: RubyAnalysis) -> Dict[str, Dict[str, Any]]:
    """
    Return file records as plain dictionaries.

    RubyAnalysis wraps parsed files, but the extractor's raw per-file records are
    the most complete source for metrics, methods, declarations, references, and
    requires. Fall back to attributes if raw is not available.
    """
    out: Dict[str, Dict[str, Any]] = {}

    for rel, pf in (analysis.files or {}).items():
        raw = getattr(pf, "raw", None)
        if isinstance(raw, dict):
            out[str(rel)] = raw
            continue

        out[str(rel)] = {
            "loc": getattr(pf, "loc", 0),
            "sloc": getattr(pf, "sloc", 0),
            "comment_lines": getattr(pf, "comment_lines", 0),
            "blank_lines": getattr(pf, "blank_lines", 0),
            "comment_pct": getattr(pf, "comment_pct", None),
            "size_bytes": getattr(pf, "size_bytes", 0),
            "parse_status": getattr(pf, "parse_status", "ok"),
            "methods": getattr(pf, "methods", []) or [],
            "references": getattr(pf, "references", []) or [],
            "declarations": getattr(pf, "declarations", []) or [],
            "requires": getattr(pf, "requires", []) or [],
            "problems": getattr(pf, "problems", []) or [],
        }

    return out


def _ruby_analysis_metrics(analysis: RubyAnalysis) -> Dict[str, Any]:
    """
    Merge-critical Ruby metrics.

    These must be carried in the returned fragment because the generic merge layer
    may not be able to recover them from a folder index. This prevents merged
    bundles from showing zero loc_code/sloc_code even when node-level metrics exist.
    """
    raw_files = _iter_file_raw_records(analysis)

    require_static_count = 0
    require_dynamic_count = 0
    require_total_count = 0

    for rec in raw_files.values():
        for req in rec.get("requires", []) or []:
            if not isinstance(req, dict):
                continue

            require_total_count += 1

            # Extractor marks dynamic require/load/autoload cases explicitly.
            # Some dynamic records also have no static spec.
            if req.get("dynamic") or not req.get("spec"):
                require_dynamic_count += 1
            else:
                require_static_count += 1

    loc_code = sum(_safe_int(r.get("loc")) for r in raw_files.values())
    sloc_code = sum(_safe_int(r.get("sloc")) for r in raw_files.values())
    comment_lines_code = sum(_safe_int(r.get("comment_lines")) for r in raw_files.values())
    blank_lines_code = sum(_safe_int(r.get("blank_lines")) for r in raw_files.values())

    comment_pct_code = None
    if loc_code > 0:
        comment_pct_code = comment_lines_code / loc_code

    return {
        "metrics_source": "ruby_analysis",
        "loc_code": loc_code,
        "sloc_code": sloc_code,
        "comment_lines_code": comment_lines_code,
        "blank_lines_code": blank_lines_code,
        "comment_pct_code": comment_pct_code,
        "metrics_code_files": len(raw_files),
        "ruby_methods_count": sum(len(r.get("methods", []) or []) for r in raw_files.values()),
        "ruby_references_count": sum(len(r.get("references", []) or []) for r in raw_files.values()),
        "ruby_declarations_count": sum(len(r.get("declarations", []) or []) for r in raw_files.values()),
        "ruby_requires_count": require_total_count,
        "ruby_requires_static_count": require_static_count,
        "ruby_requires_dynamic_count": require_dynamic_count,
    }


def _edge_kind_counts(edges: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for e in edges or []:
        if not isinstance(e, dict):
            continue
        kind = str(e.get("kind") or "unknown")
        out[kind] = out.get(kind, 0) + 1
    return out


def _analysis_summary(analysis: RubyAnalysis) -> Dict[str, Any]:
    meta = dict(analysis.meta or {})
    indexes = analysis.indexes or {}

    constants = indexes.get("constants") if isinstance(indexes, dict) else None
    methods = indexes.get("methods") if isinstance(indexes, dict) else None
    calls = indexes.get("calls") if isinstance(indexes, dict) else None
    requires = indexes.get("requires") if isinstance(indexes, dict) else None
    rails = indexes.get("rails") if isinstance(indexes, dict) else None

    def _total_route_count(rails_index: Any) -> int:
        if not isinstance(rails_index, dict):
            return 0
        routes = rails_index.get("routes")
        if not isinstance(routes, dict):
            return 0
        return sum(len(v) for v in routes.values() if hasattr(v, "__len__"))

    files_with_issues = sum(
        1
        for pf in (analysis.files or {}).values()
        if getattr(pf, "parse_status", "ok") not in ("ok",)
    )

    summary = {
        "schema_version": analysis.schema_version,
        "files_reported": len(analysis.files or {}),
        "files_with_issues": files_with_issues,
        "analysis_problems_count": len(analysis.problems or []),
        "parser": meta.get("parser", ""),
        "rails_detected": bool(meta.get("rails_detected", False)),
        "constant_declared_count": _nested_len(constants, "declared"),
        "constant_expected_count": _nested_len(constants, "expected"),
        "constant_ambiguous_count": _nested_len(constants, "ambiguous"),
        "method_declared_count": _nested_len(methods, "declared"),
        "method_calls_by_method_count": _nested_len(calls, "calls_by_method"),
        "dynamic_calls_count": _nested_len(calls, "dynamic_calls"),
        "require_by_file_count": _nested_len(requires, "by_file"),
        "require_by_spec_count": _nested_len(requires, "by_spec"),
        "require_by_provider_count": _nested_len(requires, "by_provider"),
        "rails_routes_count": _total_route_count(rails),
    }

    # Include rollup metrics in the summary as a convenient diagnostic snapshot.
    summary.update(_ruby_analysis_metrics(analysis))
    return summary


def _analysis_to_raw_json(analysis: RubyAnalysis) -> Dict[str, Any]:
    """
    Build a JSON-safe raw analysis object if RubyAnalysis.raw is unavailable.
    """
    if isinstance(getattr(analysis, "raw", None), dict):
        return analysis.raw

    return {
        "schema_version": analysis.schema_version,
        "meta": analysis.meta,
        "files": {
            rel: getattr(pf, "raw", None)
            if isinstance(getattr(pf, "raw", None), dict)
            else {
                "file_path": rel,
                "loc": getattr(pf, "loc", 0),
                "sloc": getattr(pf, "sloc", 0),
                "comment_lines": getattr(pf, "comment_lines", 0),
                "blank_lines": getattr(pf, "blank_lines", 0),
                "comment_pct": getattr(pf, "comment_pct", None),
                "size_bytes": getattr(pf, "size_bytes", 0),
                "parse_status": getattr(pf, "parse_status", "ok"),
                "methods": getattr(pf, "methods", []) or [],
                "references": getattr(pf, "references", []) or [],
                "declarations": getattr(pf, "declarations", []) or [],
                "requires": getattr(pf, "requires", []) or [],
                "problems": getattr(pf, "problems", []) or [],
            }
            for rel, pf in (analysis.files or {}).items()
        },
        "indexes": analysis.indexes,
        "problems": analysis.problems,
    }


def analyze_files_ruby(
    *,
    repo_root: Path,
    artifact_dir: Path,
    cfg: Any,
    files: Sequence[Path],
    folder_index_name: str = "folder_index_ruby.json",
    build_edges: bool = True,
    write_nodefacts: bool = True,
) -> Dict[str, object]:
    """
    Ruby bucket analyzer entrypoint.

    Pipeline:
      1. External parser handoff  (pviz-ruby-extract -> RubyAnalysis)
      2. Folder index build       (declaration + require resolution -> FolderIndex)
      3. Nodefacts projection
      4. Edge projection
      5. Artifact writes
      6. Return in-process fragment {nodes, edges, meta}

    The outer lifecycle intentionally mirrors Rust/Java bucket analyzers. The
    RubyAnalysis object is treated as Ruby's parse-cache equivalent so the richer
    extractor output is not lost during folder-index/nodefacts projection.
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
    analysis_path = artifact_dir / "ruby_analysis.json"

    log_event(
        "RUBY:analyze_files_begin",
        repo_root=str(repo_root),
        artifact_dir=str(artifact_dir),
        files_in=len(files),
        build_edges=bool(build_edges),
        write_nodefacts=bool(write_nodefacts),
    )

    if not files:
        _write_empty_artifacts(
            nodefacts_path=nodefacts_path,
            edges_path=edges_path if build_edges else None,
            folder_index_path=folder_index_path,
            repo_root=repo_root,
            reason="no_ruby_files",
        )
        return _empty_meta(
            artifact_dir=artifact_dir,
            folder_index_path=folder_index_path,
            edges_path=edges_path,
            build_edges=build_edges,
        )

    # ------------------------------------------------------------------
    # STEP 1: External parser handoff
    # ------------------------------------------------------------------
    try:
        t_parse = time.perf_counter()

        analysis = parse_ruby_analysis(
            repo_root=repo_root,
            files=list(files),
            cfg=cfg,
            work_dir=artifact_dir / ".ruby_parser_work",
        )

        _write_json(analysis_path, _analysis_to_raw_json(analysis))

        summary = _analysis_summary(analysis)
        ruby_metrics = _ruby_analysis_metrics(analysis)

        log_event(
            "RUBY:parser_handoff_done",
            ms=int((time.perf_counter() - t_parse) * 1000),
            files_reported=int(summary.get("files_reported", 0) or 0),
            files_with_issues=int(summary.get("files_with_issues", 0) or 0),
            analysis_problems=int(summary.get("analysis_problems_count", 0) or 0),
            schema=str(summary.get("schema_version", "")),
            loc_code=int(ruby_metrics.get("loc_code", 0) or 0),
            sloc_code=int(ruby_metrics.get("sloc_code", 0) or 0),
            methods=int(ruby_metrics.get("ruby_methods_count", 0) or 0),
            requires=int(ruby_metrics.get("ruby_requires_count", 0) or 0),
        )

    except RubyParserUnavailable as e:
        err = f"{type(e).__name__}:{str(e)[:500]}"
        log_event("RUBY:parser_unavailable", err=err, files_in=len(files))
        _write_empty_artifacts(
            nodefacts_path=nodefacts_path,
            edges_path=edges_path if build_edges else None,
            folder_index_path=folder_index_path,
            repo_root=repo_root,
            reason="parser_unavailable",
        )
        return _failed_meta(
            artifact_dir,
            folder_index_path,
            edges_path,
            build_edges,
            len(files),
            "unavailable",
            err,
        )

    except RubyParserError as e:
        err = f"{type(e).__name__}:{str(e)[:500]}"
        log_event("RUBY:parser_error", err=err, files_in=len(files))
        _write_empty_artifacts(
            nodefacts_path=nodefacts_path,
            edges_path=edges_path if build_edges else None,
            folder_index_path=folder_index_path,
            repo_root=repo_root,
            reason="parser_error",
        )
        return _failed_meta(
            artifact_dir,
            folder_index_path,
            edges_path,
            build_edges,
            len(files),
            "parser_error",
            err,
        )

    except Exception as e:
        err = f"{type(e).__name__}:{str(e)[:500]}"
        log_event("RUBY:unexpected_error", err=err, files_in=len(files))
        _write_empty_artifacts(
            nodefacts_path=nodefacts_path,
            edges_path=edges_path if build_edges else None,
            folder_index_path=folder_index_path,
            repo_root=repo_root,
            reason="unexpected_error",
        )
        return _failed_meta(
            artifact_dir,
            folder_index_path,
            edges_path,
            build_edges,
            len(files),
            "unexpected_error",
            err,
        )

    # ------------------------------------------------------------------
    # STEP 2: Folder index
    # ------------------------------------------------------------------
    try:
        t_idx = time.perf_counter()

        idx, tables, parse_cache = build_folder_index(analysis=analysis, cfg=cfg)
        save_folder_index(idx, tables, folder_index_path)

        # Populate cfg caches for downstream consumers. This mirrors the Rust
        # cfg.rust_parse_cache handoff, but keeps Ruby's full analysis available.
        try:
            cfg.ruby_analysis_cache = analysis
            cfg.ruby_parse_cache = parse_cache
            cfg.ruby_analysis_metrics = ruby_metrics

            cfg.ruby_fq_decl_to_file = tables.fq_decl_to_file
            cfg.ruby_module_to_files = tables.module_to_files

            cfg.ruby_constant_index = dict((analysis.indexes or {}).get("constants") or {})
            cfg.ruby_method_index = dict((analysis.indexes or {}).get("methods") or {})
            cfg.ruby_call_index = dict((analysis.indexes or {}).get("calls") or {})
            cfg.ruby_require_index = dict((analysis.indexes or {}).get("requires") or {})
            cfg.ruby_rails_index = dict((analysis.indexes or {}).get("rails") or {})
        except Exception:
            pass

        idx_meta = dict(idx.meta) if isinstance(getattr(idx, "meta", None), dict) else {}

        log_event(
            "RUBY:folder_index_built",
            ms=int((time.perf_counter() - t_idx) * 1000),
            files=len(idx.files),
            fq_decl_count=len(tables.fq_decl_to_file),
            module_index_count=len(tables.module_to_files),
            require_spec_count=len(tables.require_spec_to_files),
            cache_size=len(parse_cache) if parse_cache else 0,
        )

    except Exception as e:
        err = f"{type(e).__name__}:{str(e)[:400]}"
        log_event("RUBY:folder_index_failed", err=err)
        _write_empty_artifacts(
            nodefacts_path=nodefacts_path,
            edges_path=edges_path if build_edges else None,
            folder_index_path=folder_index_path,
            repo_root=repo_root,
            reason="folder_index_failed",
        )
        return _failed_meta(
            artifact_dir,
            folder_index_path,
            edges_path,
            build_edges,
            len(files),
            "folder_index_failed",
            err,
        )

    # ------------------------------------------------------------------
    # STEP 3: Nodefacts
    # ------------------------------------------------------------------
    if write_nodefacts:
        t_nf = time.perf_counter()
        nodefacts_obj = build_nodefacts_from_folder_index(
            idx=idx,
            tables=tables,
            analysis=analysis,
            cfg=cfg,
        )

        # Normalize immediately so both disk writes and returned in-process
        # fragments are safe for JSON serialization.
        nodefacts_obj = _json_safe(nodefacts_obj)

        try:
            nf_meta = nodefacts_obj.setdefault("meta", {})
            if isinstance(nf_meta, dict):
                nf_meta.update(
                    {
                        "language": "ruby",
                        **ruby_metrics,
                    }
                )
        except Exception:
            pass

        log_event(
            "RUBY:nodefacts_built",
            ms=int((time.perf_counter() - t_nf) * 1000),
            nodes=len(nodefacts_obj.get("nodes", {}) or {}) if isinstance(nodefacts_obj, dict) else 0,
            loc_code=int(ruby_metrics.get("loc_code", 0) or 0),
            sloc_code=int(ruby_metrics.get("sloc_code", 0) or 0),
        )
    else:
        nodefacts_obj = {
            "schema_version": "nodefacts@v1.6",
            "language": "ruby",
            "nodes": {},
            "meta": {
                "language": "ruby",
                **ruby_metrics,
            },
        }

    nodefacts_nodes: Dict[str, Any] = (
        nodefacts_obj.get("nodes", {}) or {}
        if isinstance(nodefacts_obj, dict)
        else {}
    )

    # ------------------------------------------------------------------
    # STEP 4: Edges
    # ------------------------------------------------------------------
    edges_obj: Dict[str, Any] = {
        "schema_version": "edges@v1",
        "language": "ruby",
        "edges": [],
        "meta": {
            "total": 0,
            "kind_counts": {},
            "source": "ruby_folder_index",
        },
    }
    edges_payload: List[Dict[str, Any]] = []

    if build_edges:
        t_edges = time.perf_counter()
        edges_obj = build_edges_from_folder_index(
            idx=idx,
            tables=tables,
            analysis=analysis,
            cfg=cfg,
            internal_only=not bool(getattr(cfg, "ruby_include_external_edges", False)),
        )

        edges_obj = _json_safe(edges_obj)
        edges_payload = edges_obj.get("edges", []) or [] if isinstance(edges_obj, dict) else []
        edge_kind_counts = _edge_kind_counts(edges_payload)

        try:
            edge_meta = edges_obj.setdefault("meta", {})
            if isinstance(edge_meta, dict):
                edge_meta.update(
                    {
                        "total": len(edges_payload),
                        "kind_counts": edge_kind_counts,
                        "source": "ruby_folder_index",
                    }
                )
        except Exception:
            pass

        log_event(
            "RUBY:edges_built",
            edges_total=len(edges_payload),
            edges_ms=int((time.perf_counter() - t_edges) * 1000),
            edge_kinds=edge_kind_counts,
        )

# ------------------------------------------------------------------
    # STEP 5: Write artifacts
    # ------------------------------------------------------------------
    try:
        _write_json(nodefacts_path, nodefacts_obj)
    except Exception as e:
        log_event(
            "RUBY:nodefacts_write_failed",
            err=f"{type(e).__name__}:{str(e)[:200]}",
            path=str(nodefacts_path),
        )

    if build_edges:
        try:
            _write_json(edges_path, edges_obj)
        except Exception as e:
            log_event(
                "RUBY:edges_write_failed",
                err=f"{type(e).__name__}:{str(e)[:200]}",
                path=str(edges_path),
            )

    indexes_path = artifact_dir / "ruby_analysis_indexes.json"
    ruby_indexes = _json_safe(analysis.indexes or {})
    try:
        _write_json(indexes_path, ruby_indexes)
        log_event("RUBY:indexes_written", path=str(indexes_path))
    except Exception as e:
        log_event(
            "RUBY:indexes_write_failed",
            err=f"{type(e).__name__}:{str(e)[:200]}",
            path=str(indexes_path),
        )

    total_ms = int((time.perf_counter() - t0) * 1000)
    files_reported = len(idx.files)
    files_issues = int(idx_meta.get("parse_issues_count", 0) or 0)

    edge_kind_counts = _edge_kind_counts(edges_payload)
    edges_summary = edges_obj.get("meta", {}) if isinstance(edges_obj, dict) else {}
    if not isinstance(edges_summary, dict):
        edges_summary = {}

    log_event(
        "RUBY:set_dir_ready",
        nodefacts=str(nodefacts_path),
        edges=str(edges_path if build_edges else ""),
        folder_index=str(folder_index_path),
        ruby_analysis=str(analysis_path),
        ruby_indexes=str(indexes_path),
        files_in=len(files),
        files_reported=files_reported,
        nodes=len(nodefacts_nodes),
        edges_count=len(edges_payload),
        parsed_ok=files_reported - files_issues,
        parsed_err=files_issues,
        loc_code=int(ruby_metrics.get("loc_code", 0) or 0),
        sloc_code=int(ruby_metrics.get("sloc_code", 0) or 0),
        total_ms=total_ms,
    )

    # ------------------------------------------------------------------
    # Return fragment for in-process consumers / merged bundle layer
    # ------------------------------------------------------------------
    return {
        "nodes": nodefacts_nodes,
        "edges": edges_payload if build_edges else [],
        "indexes": ruby_indexes,
        "meta": _json_safe(
            {
                "analyzer": "ruby",
                "files_in": len(files),
                "files_used": files_reported,
                "parsed_ok": files_reported - files_issues,
                "parsed_err": files_issues,
                "artifact_dir": str(artifact_dir),
                "folder_index": str(folder_index_path),
                "edges_ruby": str(edges_path) if build_edges else "",
                "ruby_analysis": str(analysis_path),
                "ruby_indexes": str(indexes_path),
                "handoff_status": "ok",
                "analysis_summary": summary,
                "edges_summary": {
                    **edges_summary,
                    "total": len(edges_payload),
                    "kind_counts": edge_kind_counts,
                },
                **ruby_metrics,
                "fq_decl_index_count": len(tables.fq_decl_to_file),
                "module_index_count": len(tables.module_to_files),
                "require_spec_index_count": len(tables.require_spec_to_files),
                "parse_cache_hits": len(parse_cache) if parse_cache else 0,
                "total_ms": total_ms,
            }
        ),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _zero_ruby_metrics() -> Dict[str, Any]:
    return {
        "metrics_source": "ruby_analysis",
        "loc_code": 0,
        "sloc_code": 0,
        "comment_lines_code": 0,
        "blank_lines_code": 0,
        "comment_pct_code": None,
        "metrics_code_files": 0,
        "ruby_methods_count": 0,
        "ruby_references_count": 0,
        "ruby_declarations_count": 0,
        "ruby_requires_count": 0,
        "ruby_requires_static_count": 0,
        "ruby_requires_dynamic_count": 0,
    }


def _write_empty_artifacts(
    *,
    nodefacts_path: Path,
    edges_path: Optional[Path],
    folder_index_path: Path,
    repo_root: Path,
    reason: str,
) -> None:
    zero_metrics = _zero_ruby_metrics()

    try:
        _write_json(
            folder_index_path,
            {
                "schema": FOLDER_INDEX_SCHEMA,
                "meta": {
                    "created": "",
                    "root": str(repo_root),
                    "language": "ruby",
                    "eligible_count": "0",
                    "parsed_count": "0",
                    "internal_edge_count": "0",
                    "parse_issues_count": "0",
                    "placeholder": "true",
                    "placeholder_reason": reason,
                    **zero_metrics,
                },
                "files": {},
            },
        )
    except Exception as e:
        log_event(
            "RUBY:empty_folder_index_write_failed",
            err=f"{type(e).__name__}:{str(e)[:200]}",
        )

    try:
        _write_json(
            nodefacts_path,
            {
                "schema_version": "nodefacts@v1.6",
                "language": "ruby",
                "nodes": {},
                "meta": {
                    "placeholder": True,
                    "placeholder_reason": reason,
                    **zero_metrics,
                },
            },
        )
    except Exception as e:
        log_event(
            "RUBY:empty_nodefacts_write_failed",
            err=f"{type(e).__name__}:{str(e)[:200]}",
        )

    if edges_path is not None:
        try:
            _write_json(
                edges_path,
                {
                    "schema_version": "edges@v1",
                    "language": "ruby",
                    "edges": [],
                    "meta": {
                        "total": 0,
                        "kind_counts": {},
                        "placeholder": True,
                        "placeholder_reason": reason,
                    },
                },
            )
        except Exception as e:
            log_event(
                "RUBY:empty_edges_write_failed",
                err=f"{type(e).__name__}:{str(e)[:200]}",
            )


def _empty_meta(
    *,
    artifact_dir: Path,
    folder_index_path: Path,
    edges_path: Path,
    build_edges: bool,
) -> Dict[str, object]:
    return {
        "nodes": {},
        "edges": [],
        "meta": {
            "analyzer": "ruby",
            "files_in": 0,
            "files_used": 0,
            "parsed_ok": 0,
            "parsed_err": 0,
            "artifact_dir": str(artifact_dir),
            "folder_index": str(folder_index_path),
            "edges_ruby": str(edges_path) if build_edges else "",
            "ruby_analysis": "",
            "handoff_status": "skipped_no_files",
            "edges_summary": {"total": 0, "kind_counts": {}},
            "fq_decl_index_count": 0,
            "module_index_count": 0,
            "require_spec_index_count": 0,
            "parse_cache_hits": 0,
            "total_ms": 0,
            **_zero_ruby_metrics(),
        },
    }


def _failed_meta(
    artifact_dir: Path,
    folder_index_path: Path,
    edges_path: Path,
    build_edges: bool,
    files_in: int,
    handoff_status: str,
    error: str,
) -> Dict[str, object]:
    return {
        "nodes": {},
        "edges": [],
        "meta": {
            "analyzer": "ruby",
            "files_in": files_in,
            "files_used": 0,
            "parsed_ok": 0,
            "parsed_err": files_in,
            "artifact_dir": str(artifact_dir),
            "folder_index": str(folder_index_path),
            "edges_ruby": str(edges_path) if build_edges else "",
            "ruby_analysis": "",
            "handoff_status": handoff_status,
            "error": error,
            "edges_summary": {"total": 0, "kind_counts": {}},
            "fq_decl_index_count": 0,
            "module_index_count": 0,
            "require_spec_index_count": 0,
            "parse_cache_hits": 0,
            "total_ms": 0,
            **_zero_ruby_metrics(),
        },
    }