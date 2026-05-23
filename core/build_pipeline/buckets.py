from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
import os

from .json_io import safe_load_json
from .normalize import ensure_nodes_dict, edges_list

try:
    from diagnostics.logging import log_event
except Exception:  # pragma: no cover
    def log_event(*_a: Any, **_k: Any) -> None:
        return


def _load_discovery_manifest(manifest_path: Path) -> Optional[List[Dict[str, Any]]]:
    try:
        obj = safe_load_json(manifest_path)
    except Exception as e:
        log_event("BUCKETS:manifest_read_failed", path=str(manifest_path), err=repr(e))
        return None

    if isinstance(obj, dict):
        files = obj.get("files")
        if isinstance(files, list):
            return [x for x in files if isinstance(x, dict)]
        items = obj.get("items")
        if isinstance(items, list):
            return [x for x in items if isinstance(x, dict)]

    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]

    log_event("BUCKETS:manifest_unrecognized_shape", shape=str(type(obj)))
    return None


def _extract_rel_path(entry: Mapping[str, Any]) -> Optional[str]:
    for k in ("rel_posix", "rel", "path", "file", "filename", "name"):
        v = entry.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _coerce_optional_path(value: Any) -> Optional[Path]:
    if value is None:
        return None
    if isinstance(value, Path):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        return Path(s)
    try:
        return Path(value)
    except Exception:
        return None


def _first_path_value(*values: Any) -> Optional[Path]:
    for value in values:
        p = _coerce_optional_path(value)
        if p is not None:
            return p
    return None


def _bucket_manifest_files(
    *,
    scan_root: Path,
    manifest_files: Sequence[Mapping[str, Any]],
) -> Tuple[List[Path], List[Path], List[Path], List[Path], List[Path], List[Path], Dict[str, Any]]:
    scan_root_resolved = scan_root.resolve(strict=False)

    py_files: List[Path] = []
    ts_files: List[Path] = []
    go_files: List[Path] = []
    java_files: List[Path] = []
    rust_files: List[Path] = []
    kotlin_files: List[Path] = []

    dropped_outside_root = 0
    dropped_missing_rel = 0
    skipped_non_code = 0

    for ent in manifest_files:
        rel = _extract_rel_path(ent)
        if not rel:
            dropped_missing_rel += 1
            continue

        p = (scan_root / rel).resolve(strict=False)
        try:
            p.relative_to(scan_root_resolved)
        except Exception:
            dropped_outside_root += 1
            continue

        suf = p.suffix.lower()
        if suf == ".py":
            py_files.append(p)
        elif suf in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
            ts_files.append(p)
        elif suf == ".go":
            go_files.append(p)
        elif suf == ".java":
            java_files.append(p)
        elif suf == ".rs":
            rust_files.append(p)
        elif suf in (".kt", ".kts"):
            kotlin_files.append(p)
        else:
            skipped_non_code += 1

    summary = {
        "py_files": len(py_files),
        "ts_files": len(ts_files),
        "go_files": len(go_files),
        "java_files": len(java_files),
        "rust_files": len(rust_files),
        "kotlin_files": len(kotlin_files),
        "dropped_outside_root": dropped_outside_root,
        "dropped_missing_rel": dropped_missing_rel,
        "skipped_non_code": skipped_non_code,
    }
    return py_files, ts_files, go_files, java_files, rust_files, kotlin_files, summary


def _read_lang_artifacts(lang_dir: Path) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    nodes: Dict[str, Dict[str, Any]] = {}
    edges: List[Dict[str, Any]] = []
    folder_index: Optional[Dict[str, Any]] = None

    nf_p = lang_dir / "nodefacts.json"
    e_p = lang_dir / "edges.json"
    fi_p = lang_dir / "folder_index.json"

    if nf_p.exists():
        try:
            nf_obj = safe_load_json(nf_p)
            nodes = ensure_nodes_dict(nf_obj.get("nodes") if isinstance(nf_obj, dict) else nf_obj)
        except Exception as e:
            log_event("BUCKETS:read_nodefacts_failed", path=str(nf_p), err=repr(e))

    if e_p.exists():
        try:
            edges = edges_list(safe_load_json(e_p))
        except Exception as e:
            log_event("BUCKETS:read_edges_failed", path=str(e_p), err=repr(e))

    if fi_p.exists():
        try:
            fi_obj = safe_load_json(fi_p)
            if isinstance(fi_obj, dict):
                folder_index = fi_obj
        except Exception as e:
            log_event("BUCKETS:read_folder_index_failed", path=str(fi_p), err=repr(e))

    return nodes, edges, folder_index


def merge_folder_indexes(
    py_fi: Optional[Dict[str, Any]] = None,
    ts_fi: Optional[Dict[str, Any]] = None,
    go_fi: Optional[Dict[str, Any]] = None,
    java_fi: Optional[Dict[str, Any]] = None,
    kotlin_fi: Optional[Dict[str, Any]] = None,
    rust_fi: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    indexes = [fi for fi in [py_fi, ts_fi, go_fi, java_fi, kotlin_fi, rust_fi] if fi is not None]
    if not indexes:
        return None

    if len(indexes) == 1:
        return indexes[0]

    out = dict(indexes[0])

    def _as_dict(x: Any) -> Dict[str, Any]:
        if isinstance(x, dict):
            return dict(x)
        if isinstance(x, list):
            d: Dict[str, Any] = {}
            for it in x:
                if not isinstance(it, dict):
                    continue
                rid = (
                    it.get("rel_posix")
                    or it.get("path")
                    or it.get("id")
                    or it.get("file")
                    or it.get("folder_id")
                )
                if rid:
                    d[str(rid)] = it
            return d
        return {}

    merged_files: Dict[str, Any] = {}

    for idx in indexes:
        files = idx.get("files")
        if files:
            merged_files.update(_as_dict(files))

    out["files"] = merged_files

    if "meta" in out:
        meta = dict(out["meta"])

        total_eligible = 0
        total_parsed = 0
        total_errors = 0
        languages = set()

        for idx in indexes:
            idx_meta = idx.get("meta", {})

            lang = idx_meta.get("language")
            if lang:
                languages.add(str(lang))

            try:
                total_eligible += int(idx_meta.get("eligible_count", 0) or 0)
            except (ValueError, TypeError):
                pass

            try:
                total_parsed += int(idx_meta.get("parsed_count", 0) or 0)
            except (ValueError, TypeError):
                pass

            try:
                total_errors += int(idx_meta.get("parse_issues_count", 0) or idx_meta.get("parse_err", 0) or 0)
            except (ValueError, TypeError):
                pass

        meta["languages"] = sorted(languages)
        meta["eligible_count"] = str(total_eligible)
        meta["parsed_count"] = str(total_parsed)
        meta["parse_issues_count"] = str(total_errors)

        out["meta"] = meta

    return out


def run_bucket_analyzers_default(
    *,
    scan_root: Path,
    store_root: Path,
    artifacts_dir: Path,
    cfg_like: Any,
    manifest_path: Optional[Path],
) -> Optional[Dict[str, Any]]:
    import time
    from collections import Counter

    def _safe_stat(p: Path) -> dict:
        try:
            st = p.stat()
            return {"exists": True, "size": int(st.st_size), "mtime": float(st.st_mtime)}
        except Exception:
            return {"exists": False, "size": 0, "mtime": 0.0}

    def _list_dir(p: Path, *, max_items: int = 50) -> dict:
        try:
            if not p.exists():
                return {"exists": False, "files": [], "count": 0}
            files = []
            for f in sorted(p.glob("*"))[:max_items]:
                try:
                    files.append({"name": f.name, "size": int(f.stat().st_size)})
                except Exception:
                    files.append({"name": f.name, "size": None})
            try:
                count = sum(1 for _ in p.glob("*"))
            except Exception:
                count = len(files)
            return {"exists": True, "files": files, "count": int(count)}
        except Exception as e:
            return {"exists": False, "err": repr(e), "files": [], "count": 0}

    if not manifest_path:
        log_event("BUCKETS:skip_no_manifest_path")
        return None

    if not manifest_path.exists():
        log_event("BUCKETS:skip_missing_manifest", path=str(manifest_path))
        return None

    t0 = time.time()
    log_event(
        "BUCKETS:begin",
        scan_root=str(scan_root),
        store_root=str(store_root),
        artifacts_dir=str(artifacts_dir),
        manifest_path=str(manifest_path),
        manifest_stat=_safe_stat(manifest_path),
    )

    files = _load_discovery_manifest(manifest_path)
    if not files:
        log_event("BUCKETS:skip_empty_manifest", path=str(manifest_path))
        return None

    try:
        ext_counts = Counter(
            Path(_extract_rel_path(f) or "").suffix.lower()
            for f in files
            if isinstance(f, dict)
        )
        top_ext = dict(ext_counts.most_common(12))
    except Exception as e:
        top_ext = {"err": repr(e)}

    log_event(
        "BUCKETS:manifest_loaded",
        path=str(manifest_path),
        total_files=int(len(files)),
        ext_top=top_ext,
        sample_files=[str(x) for x in (files[:5] if isinstance(files, list) else [])],
    )

    t_bucket0 = time.time()
    bucketed = _bucket_manifest_files(
        scan_root=scan_root,
        manifest_files=files,
    )
    t_bucket1 = time.time()

    if isinstance(bucketed, tuple) and len(bucketed) == 7:
        py_files, ts_files, go_files, java_files, rust_files, kotlin_files, bucket_summary = bucketed
        log_event("BUCKETS:bucket_version", shape=7, kotlin_enabled=True)
    elif isinstance(bucketed, tuple) and len(bucketed) == 6:
        py_files, ts_files, go_files, java_files, rust_files, bucket_summary = bucketed
        kotlin_files = []
        log_event("BUCKETS:bucket_version_compat", shape=6, kotlin_defaulted=True)
    elif isinstance(bucketed, tuple) and len(bucketed) == 5:
        py_files, ts_files, go_files, java_files, bucket_summary = bucketed
        rust_files = []
        kotlin_files = []
        log_event("BUCKETS:bucket_version_compat", shape=5, rust_kotlin_defaulted=True)
    elif isinstance(bucketed, tuple) and len(bucketed) == 4:
        py_files, ts_files, go_files, bucket_summary = bucketed
        java_files = []
        rust_files = []
        kotlin_files = []
        log_event("BUCKETS:bucket_version_compat", shape=4, java_rust_kotlin_defaulted=True)
    else:
        log_event(
            "BUCKETS:bucketed_unexpected_shape",
            shape=repr(type(bucketed)),
            repr_bucketed=repr(bucketed)[:500],
        )
        return None

    log_event("BUCKETS:bucketed", **(bucket_summary or {}))
    log_event(
        "BUCKETS:bucket_counts",
        elapsed_ms=int((t_bucket1 - t_bucket0) * 1000),
        py=int(len(py_files) if py_files else 0),
        ts=int(len(ts_files) if ts_files else 0),
        go=int(len(go_files) if go_files else 0),
        java=int(len(java_files) if java_files else 0),
        rust=int(len(rust_files) if rust_files else 0),
        kotlin=int(len(kotlin_files) if kotlin_files else 0),
        rust_sample=str(rust_files[0]) if rust_files else "",
        kotlin_sample=str(kotlin_files[0]) if kotlin_files else "",
        java_sample=str(java_files[0]) if java_files else "",
        go_sample=str(go_files[0]) if go_files else "",
        ts_sample=str(ts_files[0]) if ts_files else "",
    )

    analyzers_root = artifacts_dir / "analyzers"

    ts_out = analyzers_root / "ts"
    ts_out.mkdir(parents=True, exist_ok=True)

    go_out = analyzers_root / "go"
    go_out.mkdir(parents=True, exist_ok=True)

    java_out = analyzers_root / "java"
    java_out.mkdir(parents=True, exist_ok=True)

    rust_out = analyzers_root / "rust"
    rust_out.mkdir(parents=True, exist_ok=True)

    kotlin_out = analyzers_root / "kotlin"
    kotlin_out.mkdir(parents=True, exist_ok=True)

    def _default_goextract_bin(scan_root: Path) -> Optional[Path]:
        candidates = [
            scan_root / "backend" / "tools" / "goextract" / "goextract.exe",
            scan_root / "backend" / "tools" / "goextract" / "goextract",
            scan_root / "tools" / "goextract" / "goextract.exe",
            scan_root / "tools" / "goextract" / "goextract",
        ]

        for p in candidates:
            try:
                if p.exists() and p.is_file():
                    return p
            except Exception:
                continue

        return None

    log_event(
        "BUCKETS:out_dirs_ready",
        analyzers_root=str(analyzers_root),
        ts_out=str(ts_out),
        go_out=str(go_out),
        java_out=str(java_out),
        rust_out=str(rust_out),
        kotlin_out=str(kotlin_out),
        analyzers_root_listing=_list_dir(analyzers_root, max_items=30),
    )

    if py_files:
        log_event(
            "BUCKETS:python_deferred",
            reason="python_runs_after_buckets",
            files=len(py_files),
        )

    ts_ok = False
    if ts_files:
        t_ts0 = time.time()
        try:
            from analyzer.ts.run import analyze_files_ts  # type: ignore
            from analyzer.ts.config import TSAnalyzerCfg  # type: ignore

            if isinstance(cfg_like, TSAnalyzerCfg):
                ts_cfg = cfg_like
            else:
                defaults = TSAnalyzerCfg()
                base = cfg_like
                ts_cfg = TSAnalyzerCfg(
                    max_bytes_per_file=getattr(
                        base,
                        "max_bytes_per_file",
                        getattr(base, "max_file_bytes", defaults.max_bytes_per_file),
                    ),
                    skip_d_ts=getattr(base, "skip_d_ts", defaults.skip_d_ts),
                    treat_slash_root_as_repo_root=getattr(
                        base,
                        "treat_slash_root_as_repo_root",
                        defaults.treat_slash_root_as_repo_root,
                    ),
                    emit_external_edges=getattr(base, "emit_external_edges", defaults.emit_external_edges),
                    nodefacts_name=getattr(base, "nodefacts_name", defaults.nodefacts_name),
                    edges_name=getattr(base, "edges_name", defaults.edges_name),
                    folder_index_name=getattr(base, "folder_index_name", defaults.folder_index_name),
                    language_bundle_path=getattr(base, "language_bundle_path", defaults.language_bundle_path),
                    include_globs=getattr(base, "include_globs", defaults.include_globs),
                    exclude_globs=getattr(base, "exclude_globs", defaults.exclude_globs),
                )

            log_event(
                "BUCKETS:ts_start",
                out=str(ts_out),
                files=int(len(ts_files)),
                cfg={
                    "max_bytes_per_file": getattr(ts_cfg, "max_bytes_per_file", None),
                    "skip_d_ts": getattr(ts_cfg, "skip_d_ts", None),
                    "emit_external_edges": getattr(ts_cfg, "emit_external_edges", None),
                    "nodefacts_name": getattr(ts_cfg, "nodefacts_name", None),
                    "edges_name": getattr(ts_cfg, "edges_name", None),
                    "folder_index_name": getattr(ts_cfg, "folder_index_name", None),
                },
            )

            analyze_files_ts(
                repo_root=scan_root,
                artifact_dir=ts_out,
                cfg=ts_cfg,
                files=ts_files,
            )
            ts_ok = True
            log_event(
                "BUCKETS:ts_done",
                out=str(ts_out),
                files=len(ts_files),
                elapsed_ms=int((time.time() - t_ts0) * 1000),
                out_listing=_list_dir(ts_out, max_items=30),
            )
        except Exception as e:
            log_event(
                "BUCKETS:ts_failed",
                err=repr(e),
                files=len(ts_files),
                elapsed_ms=int((time.time() - t_ts0) * 1000),
                out_listing=_list_dir(ts_out, max_items=20),
            )
    else:
        log_event("BUCKETS:ts_skipped", reason="no_ts_files", out=str(ts_out))

    go_ok = False
    if go_files:
        t_go0 = time.time()
        try:
            from analyzer.go.go_run import analyze_files_go  # type: ignore
            from analyzer.go.go_config import GoAnalyzerCfg  # type: ignore

            env_goextract_bin = os.environ.get("PVIZ_GOEXTRACT_BIN", "")
            env_goextract_path = os.environ.get("PVIZ_GOEXTRACT_PATH", "")

            if isinstance(cfg_like, GoAnalyzerCfg):
                go_cfg = cfg_like
                if getattr(go_cfg, "goextract_bin", None) is None:
                    env_bin = _first_path_value(
                        env_goextract_bin,
                        env_goextract_path,
                        _default_goextract_bin(scan_root),
                    )
                    if env_bin is not None:
                        try:
                            go_cfg.goextract_bin = env_bin
                        except Exception:
                            pass
            else:
                defaults = GoAnalyzerCfg()
                base = cfg_like

                goextract_bin = _first_path_value(
                    getattr(base, "goextract_bin", None),
                    getattr(base, "goextract_path", None),
                    getattr(base, "go_extractor_bin", None),
                    env_goextract_bin,
                    env_goextract_path,
                    defaults.goextract_bin,
                    _default_goextract_bin(scan_root),
                )

                go_cfg = GoAnalyzerCfg(
                    max_file_bytes=getattr(
                        base,
                        "max_file_bytes",
                        getattr(base, "max_bytes_per_file", defaults.max_file_bytes),
                    ),
                    max_bytes_per_file=getattr(
                        base,
                        "max_bytes_per_file",
                        getattr(base, "max_file_bytes", defaults.max_bytes_per_file),
                    ),
                    include_tests=getattr(base, "include_tests", defaults.include_tests),
                    include_generated=getattr(base, "include_generated", defaults.include_generated),
                    allow_oversize_files=getattr(base, "allow_oversize_files", defaults.allow_oversize_files),
                    respect_go_mod=getattr(base, "respect_go_mod", defaults.respect_go_mod),
                    package_level_graph=getattr(base, "package_level_graph", defaults.package_level_graph),
                    include_external_edges=getattr(
                        base,
                        "include_external_edges",
                        defaults.include_external_edges,
                    ),
                    enable_go_ast=getattr(base, "enable_go_ast", defaults.enable_go_ast),
                    goextract_bin=goextract_bin,
                    goextract_timeout_s=getattr(
                        base,
                        "goextract_timeout_s",
                        defaults.goextract_timeout_s,
                    ),
                    goextract_include_docs=getattr(
                        base,
                        "goextract_include_docs",
                        defaults.goextract_include_docs,
                    ),
                    goextract_include_imports=getattr(
                        base,
                        "goextract_include_imports",
                        defaults.goextract_include_imports,
                    ),
                    goextract_include_build=getattr(
                        base,
                        "goextract_include_build",
                        defaults.goextract_include_build,
                    ),
                    goextract_batch_size=getattr(
                        base,
                        "goextract_batch_size",
                        defaults.goextract_batch_size,
                    ),
                    nodefacts_name=getattr(base, "nodefacts_name", defaults.nodefacts_name),
                    edges_name=getattr(base, "edges_name", defaults.edges_name),
                    folder_index_name=getattr(base, "folder_index_name", defaults.folder_index_name),
                    emit_go_details=getattr(base, "emit_go_details", defaults.emit_go_details),
                    go_details_name=getattr(base, "go_details_name", defaults.go_details_name),
                    go_edge_reasons_name=getattr(
                        base,
                        "go_edge_reasons_name",
                        defaults.go_edge_reasons_name,
                    ),
                    go_details_include_symbols=getattr(
                        base,
                        "go_details_include_symbols",
                        defaults.go_details_include_symbols,
                    ),
                    go_details_include_import_resolution=getattr(
                        base,
                        "go_details_include_import_resolution",
                        defaults.go_details_include_import_resolution,
                    ),
                    go_details_include_build_flags=getattr(
                        base,
                        "go_details_include_build_flags",
                        defaults.go_details_include_build_flags,
                    ),
                    emit_edge_reasons=getattr(base, "emit_edge_reasons", defaults.emit_edge_reasons),
                    max_reasons_per_edge=getattr(base, "max_reasons_per_edge", defaults.max_reasons_per_edge),
                    enable_go_list=getattr(base, "enable_go_list", defaults.enable_go_list),
                    go_binary=_first_path_value(
                        getattr(base, "go_binary", None),
                        defaults.go_binary,
                    ),
                    include_globs=getattr(base, "include_globs", defaults.include_globs),
                    exclude_globs=getattr(base, "exclude_globs", defaults.exclude_globs),
                )

            cfg_goextract_bin = getattr(go_cfg, "goextract_bin", None)
            cfg_goextract_bin_path = _coerce_optional_path(cfg_goextract_bin)
            cfg_goextract_stat = (
                _safe_stat(cfg_goextract_bin_path)
                if cfg_goextract_bin_path is not None
                else {"exists": False, "size": 0, "mtime": 0.0}
            )

            log_event(
                "BUCKETS:go_start",
                out=str(go_out),
                files=int(len(go_files)),
                cfg={
                    "max_file_bytes": getattr(go_cfg, "max_file_bytes", None),
                    "max_bytes_per_file": getattr(go_cfg, "max_bytes_per_file", None),
                    "include_tests": getattr(go_cfg, "include_tests", None),
                    "include_generated": getattr(go_cfg, "include_generated", None),
                    "respect_go_mod": getattr(go_cfg, "respect_go_mod", None),
                    "package_level_graph": getattr(go_cfg, "package_level_graph", None),
                    "include_external_edges": getattr(go_cfg, "include_external_edges", None),
                    "enable_go_ast": getattr(go_cfg, "enable_go_ast", None),
                    "enable_go_list": getattr(go_cfg, "enable_go_list", None),
                    "goextract_bin": str(cfg_goextract_bin or ""),
                    "goextract_bin_stat": cfg_goextract_stat,
                    "goextract_env": env_goextract_bin,
                    "goextract_path_env": env_goextract_path,
                    "go_binary": str(getattr(go_cfg, "go_binary", "") or ""),
                    "folder_index_name": getattr(go_cfg, "folder_index_name", None),
                },
            )

            frag = analyze_files_go(
                repo_root=scan_root,
                artifact_dir=go_out,
                cfg=go_cfg,
                files=go_files,
                folder_index_name=getattr(go_cfg, "folder_index_name", "folder_index_go.json"),
            )
            go_ok = True
            log_event(
                "BUCKETS:go_done",
                out=str(go_out),
                files=len(go_files),
                elapsed_ms=int((time.time() - t_go0) * 1000),
                folder_index=str(Path(go_out) / getattr(go_cfg, "folder_index_name", "folder_index_go.json")),
                module_path=str((frag.get("meta") or {}).get("module_path") or "") if isinstance(frag, dict) else "",
                nodes=(len(frag.get("nodes") or {}) if isinstance(frag, dict) else 0),
                edges=(len(frag.get("edges") or []) if isinstance(frag, dict) else 0),
                out_listing=_list_dir(go_out, max_items=30),
            )
        except Exception as e:
            log_event(
                "BUCKETS:go_failed",
                err=repr(e),
                files=len(go_files),
                elapsed_ms=int((time.time() - t_go0) * 1000),
                out_listing=_list_dir(go_out, max_items=20),
            )
    else:
        log_event("BUCKETS:go_skipped", reason="no_go_files", out=str(go_out))

    java_ok = False
    if java_files:
        t_java0 = time.time()
        try:
            from analyzer.java.java_run import analyze_files_java  # type: ignore
            from analyzer.java.java_config import JavaAnalyzerCfg  # type: ignore

            if isinstance(cfg_like, JavaAnalyzerCfg):
                java_cfg = cfg_like
            else:
                defaults = JavaAnalyzerCfg()
                base = cfg_like
                java_cfg = JavaAnalyzerCfg(
                    include_tests=getattr(base, "include_tests", defaults.include_tests),
                    include_generated=getattr(base, "include_generated", defaults.include_generated),
                )

            java_engine = str(getattr(cfg_like, "java_engine", "") or "").strip()
            env_engine = os.environ.get("PVIZ_JAVA_PARSER_ENGINE", "")
            env_jar = os.environ.get("PVIZ_JAVAPARSER_JAR", "")
            jar_stat = _safe_stat(Path(env_jar)) if env_jar else {"exists": False, "size": 0, "mtime": 0.0}

            log_event(
                "BUCKETS:java_start",
                out=str(java_out),
                files=int(len(java_files)),
                cfg={
                    "include_tests": getattr(java_cfg, "include_tests", None),
                    "include_generated": getattr(java_cfg, "include_generated", None),
                    "folder_index_name": getattr(java_cfg, "folder_index_name", None),
                    "java_engine_cfg": java_engine,
                    "java_engine_env": env_engine,
                    "javaparser_jar_env": env_jar,
                    "javaparser_jar_stat": jar_stat,
                },
                sample=str(java_files[0]) if java_files else "",
            )

            res = analyze_files_java(
                repo_root=scan_root,
                artifact_dir=java_out,
                cfg=java_cfg,
                files=java_files,
                folder_index_name=getattr(java_cfg, "folder_index_name", "folder_index_java.json"),
            )
            java_ok = True
            log_event(
                "BUCKETS:java_done",
                out=str(java_out),
                files=len(java_files),
                elapsed_ms=int((time.time() - t_java0) * 1000),
                folder_index=str(getattr(res, "folder_index_path", "")),
                edges=str(getattr(res, "edges_path", "") or ""),
                out_listing=_list_dir(java_out, max_items=30),
            )
        except Exception as e:
            log_event(
                "BUCKETS:java_failed",
                err=repr(e),
                files=len(java_files),
                elapsed_ms=int((time.time() - t_java0) * 1000),
                out_listing=_list_dir(java_out, max_items=20),
            )
    else:
        log_event("BUCKETS:java_skipped", reason="no_java_files", out=str(java_out))

    rust_ok = False
    if rust_files:
        t_rust0 = time.time()
        try:
            from analyzer.rust.rust_run import analyze_files_rust  # type: ignore
            from analyzer.rust.rust_config import RustAnalyzerCfg  # type: ignore

            if isinstance(cfg_like, RustAnalyzerCfg):
                rust_cfg = cfg_like
            else:
                defaults = RustAnalyzerCfg()
                base = cfg_like
                rust_cfg = RustAnalyzerCfg(
                    max_bytes_per_file=getattr(
                        base,
                        "max_bytes_per_file",
                        getattr(base, "max_file_bytes", defaults.max_bytes_per_file),
                    ),
                    include_tests=getattr(base, "include_tests", defaults.include_tests),
                    rustparser_cli_path=getattr(base, "rustparser_cli_path", defaults.rustparser_cli_path),
                    nodefacts_name=getattr(base, "nodefacts_name", defaults.nodefacts_name),
                    edges_name=getattr(base, "edges_name", defaults.edges_name),
                    folder_index_name=getattr(base, "folder_index_name", defaults.folder_index_name),
                )

            env_cli = os.environ.get("PVIZ_RUSTPARSER_BIN", "")
            cli_stat = _safe_stat(Path(env_cli)) if env_cli else {"exists": False, "size": 0, "mtime": 0.0}

            log_event(
                "BUCKETS:rust_start",
                out=str(rust_out),
                files=int(len(rust_files)),
                cfg={
                    "max_bytes_per_file": getattr(rust_cfg, "max_bytes_per_file", None),
                    "include_tests": getattr(rust_cfg, "include_tests", None),
                    "rustparser_cli_path": getattr(rust_cfg, "rustparser_cli_path", None),
                    "folder_index_name": getattr(rust_cfg, "folder_index_name", None),
                    "rustparser_cli_env": env_cli,
                    "rustparser_cli_stat": cli_stat,
                },
                sample=str(rust_files[0]) if rust_files else "",
            )

            frag = analyze_files_rust(
                repo_root=scan_root,
                artifact_dir=rust_out,
                cfg=rust_cfg,
                files=rust_files,
                folder_index_name=getattr(rust_cfg, "folder_index_name", "folder_index_rust.json"),
                build_edges=True,
                write_nodefacts=True,
            )
            rust_ok = True
            log_event(
                "BUCKETS:rust_done",
                out=str(rust_out),
                files=len(rust_files),
                elapsed_ms=int((time.time() - t_rust0) * 1000),
                nodes=(len(frag.get("nodes") or {}) if isinstance(frag, dict) else 0),
                edges=(len(frag.get("edges") or []) if isinstance(frag, dict) else 0),
                out_listing=_list_dir(rust_out, max_items=30),
            )
        except Exception as e:
            log_event(
                "BUCKETS:rust_failed",
                err=repr(e),
                files=len(rust_files),
                elapsed_ms=int((time.time() - t_rust0) * 1000),
                out_listing=_list_dir(rust_out, max_items=20),
            )
    else:
        log_event("BUCKETS:rust_skipped", reason="no_rust_files", out=str(rust_out))

    kotlin_ok = False
    if kotlin_files:
        t_kotlin0 = time.time()
        try:
            from analyzer.kotlin.kotlin_run import analyze_files_kotlin  # type: ignore
            from analyzer.kotlin.kotlin_config import KotlinAnalyzerCfg  # type: ignore

            if isinstance(cfg_like, KotlinAnalyzerCfg):
                kotlin_cfg = cfg_like
            else:
                defaults = KotlinAnalyzerCfg()
                base = cfg_like
                kotlin_cfg = KotlinAnalyzerCfg(
                    max_file_bytes=getattr(
                        base,
                        "max_file_bytes",
                        getattr(base, "max_bytes_per_file", defaults.max_file_bytes),
                    ),
                    max_bytes_per_file=getattr(
                        base,
                        "max_bytes_per_file",
                        getattr(base, "max_file_bytes", defaults.max_bytes_per_file),
                    ),
                    include_tests=getattr(base, "include_tests", defaults.include_tests),
                    include_kts=getattr(base, "include_kts", defaults.include_kts),
                    include_generated=getattr(base, "include_generated", defaults.include_generated),
                    kotlinparser_cli_path=getattr(base, "kotlinparser_cli_path", defaults.kotlinparser_cli_path),
                    kotlin_node_id_space=getattr(base, "kotlin_node_id_space", defaults.kotlin_node_id_space),
                    nodefacts_name=getattr(base, "nodefacts_name", defaults.nodefacts_name),
                    edges_name=getattr(base, "edges_name", defaults.edges_name),
                    folder_index_name=getattr(base, "folder_index_name", defaults.folder_index_name),
                    kotlinparser_timeout_s=getattr(base, "kotlinparser_timeout_s", defaults.kotlinparser_timeout_s),
                )

            env_cli = os.environ.get("PVIZ_KOTLINPARSER_BIN", "")
            cli_stat = _safe_stat(Path(env_cli)) if env_cli else {"exists": False, "size": 0, "mtime": 0.0}

            log_event(
                "BUCKETS:kotlin_start",
                out=str(kotlin_out),
                files=int(len(kotlin_files)),
                cfg={
                    "max_file_bytes": getattr(kotlin_cfg, "max_file_bytes", None),
                    "max_bytes_per_file": getattr(kotlin_cfg, "max_bytes_per_file", None),
                    "include_tests": getattr(kotlin_cfg, "include_tests", None),
                    "include_kts": getattr(kotlin_cfg, "include_kts", None),
                    "include_generated": getattr(kotlin_cfg, "include_generated", None),
                    "kotlinparser_cli_path": getattr(kotlin_cfg, "kotlinparser_cli_path", None),
                    "kotlin_node_id_space": getattr(kotlin_cfg, "kotlin_node_id_space", None),
                    "folder_index_name": getattr(kotlin_cfg, "folder_index_name", None),
                    "kotlinparser_cli_env": env_cli,
                    "kotlinparser_cli_stat": cli_stat,
                },
                sample=str(kotlin_files[0]) if kotlin_files else "",
            )

            frag = analyze_files_kotlin(
                repo_root=scan_root,
                artifact_dir=kotlin_out,
                cfg=kotlin_cfg,
                files=kotlin_files,
                folder_index_name=getattr(kotlin_cfg, "folder_index_name", "folder_index_kotlin.json"),
                build_edges=True,
                write_nodefacts=True,
            )
            kotlin_ok = True
            log_event(
                "BUCKETS:kotlin_done",
                out=str(kotlin_out),
                files=len(kotlin_files),
                elapsed_ms=int((time.time() - t_kotlin0) * 1000),
                nodes=(len(frag.get("nodes") or {}) if isinstance(frag, dict) else 0),
                edges=(len(frag.get("edges") or []) if isinstance(frag, dict) else 0),
                out_listing=_list_dir(kotlin_out, max_items=30),
            )
        except Exception as e:
            log_event(
                "BUCKETS:kotlin_failed",
                err=repr(e),
                files=len(kotlin_files),
                elapsed_ms=int((time.time() - t_kotlin0) * 1000),
                out_listing=_list_dir(kotlin_out, max_items=20),
            )
    else:
        log_event("BUCKETS:kotlin_skipped", reason="no_kotlin_files", out=str(kotlin_out))

    log_event(
        "BUCKETS:end",
        elapsed_ms=int((time.time() - t0) * 1000),
        ts_ok=bool(ts_ok),
        go_ok=bool(go_ok),
        java_ok=bool(java_ok),
        rust_ok=bool(rust_ok),
        kotlin_ok=bool(kotlin_ok),
        ts_out_listing=_list_dir(ts_out, max_items=15),
        go_out_listing=_list_dir(go_out, max_items=15),
        java_out_listing=_list_dir(java_out, max_items=15),
        rust_out_listing=_list_dir(rust_out, max_items=15),
        kotlin_out_listing=_list_dir(kotlin_out, max_items=15),
    )

    return {
        "meta": {
            "scan_root": str(scan_root),
            "store_root": str(store_root),
            "mode": "bucket_analyzers_default_ts_go_java_rust_kotlin",
            "bucket_summary": bucket_summary,

            "ts_out": str(ts_out),
            "ts_ok": bool(ts_ok),
            "ts_files": int(len(ts_files) if ts_files else 0),

            "go_out": str(go_out),
            "go_ok": bool(go_ok),
            "go_files": int(len(go_files) if go_files else 0),

            "java_out": str(java_out),
            "java_ok": bool(java_ok),
            "java_files": int(len(java_files) if java_files else 0),

            "rust_out": str(rust_out),
            "rust_ok": bool(rust_ok),
            "rust_files": int(len(rust_files) if rust_files else 0),

            "kotlin_out": str(kotlin_out),
            "kotlin_ok": bool(kotlin_ok),
            "kotlin_files": int(len(kotlin_files) if kotlin_files else 0),

            "py_files": int(len(py_files) if py_files else 0),
        },
        "nodes": {},
        "edges": [],
    }