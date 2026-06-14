from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, cast, List
 
from ..build_context import BuildContext
from .types import (
    AnalyzerBuildResult,
    InitArtifactsCtxFn,
    AttachWorkspaceDirFn,
    FollowUpAnalyzerFn,
    RunBucketAnalyzersFn,
    PrepareZonesModeFn,
    CleanupTempFn,
    BuildZonesFn,
    RunClassicBuildFn,
    ZonesLoggerFn,
)
from .json_io import safe_load_json
from .normalize import ensure_nodes_dict, edges_list
from .buckets import _load_discovery_manifest
from .followups import maybe_run_ts_followup

try:
    from diagnostics.logging import log_event
except Exception:  # pragma: no cover
    def log_event(*_a: Any, **_k: Any) -> None:
        return


def _default_cleanup_tmp(tmp_root: Path) -> None:
    if not tmp_root.exists():
        return
    for p in sorted(tmp_root.rglob("*"), reverse=True):
        try:
            if p.is_file() or p.is_symlink():
                p.unlink(missing_ok=True)
            elif p.is_dir():
                p.rmdir()
        except Exception:
            pass

def _norm_from_canonical(artifacts_dir: Path) -> Optional[Dict[str, Any]]:
    try:
        # ------------------------------------------------------------------
        # Priority 1: root canonical artifacts (written by publish_canonical_from_sets).
        # This is the expected path for any repo that ran the full pipeline.
        # ------------------------------------------------------------------
        if (artifacts_dir / "nodefacts.json").exists():
            nf_p = artifacts_dir / "nodefacts.json"

            # Prefer root edges.json; fall back to classic/edges/full.json.
            if (artifacts_dir / "edges.json").exists():
                e_p = artifacts_dir / "edges.json"
            elif (artifacts_dir / "classic" / "edges" / "full.json").exists():
                e_p = artifacts_dir / "classic" / "edges" / "full.json"
            else:
                e_p = artifacts_dir / "edges.json"   # may not exist; handled below

            loaded_base = artifacts_dir

        else:
            # ------------------------------------------------------------------
            # Priority 2: per-language analyzer subdirs, in descending preference.
            #
            # Each entry is (base_dir, [edge_filename_candidates]).
            # Edge candidates are tried in order; first existing file wins.
            # Language-prefixed names (edges_go.json) are tried before the
            # generic fallback (edges.json) so the right artifact is always used.
            #
            # Legacy flat dirs (ts/, go/, java/) are included for repos that were
            # scanned before the analyzers/ prefix convention was introduced.
            # Kotlin, Rust, and Ruby never had legacy paths.
            # ------------------------------------------------------------------
            lang_candidates: list = [
                # Current convention
                (artifacts_dir / "analyzers" / "go",
                    ["edges_go.json", "edges.json"]),
                (artifacts_dir / "analyzers" / "java",
                    ["edges_java.json", "edges.json"]),
                (artifacts_dir / "analyzers" / "kotlin",
                    ["edges_kotlin.json", "edges.json"]),
                (artifacts_dir / "analyzers" / "rust",
                    ["edges_rust.json", "edges.json"]),
                (artifacts_dir / "analyzers" / "ruby",
                    ["edges.json"]),
                (artifacts_dir / "analyzers" / "ts",
                    ["edges.json"]),
                # Legacy flat dirs
                (artifacts_dir / "go",
                    ["edges_go.json", "edges.json"]),
                (artifacts_dir / "java",
                    ["edges_java.json", "edges.json"]),
                (artifacts_dir / "ts",
                    ["edges.json"]),
            ]

            nf_p = artifacts_dir / "nodefacts.json"   # default; overwritten below
            e_p = artifacts_dir / "edges.json"         # default; overwritten below
            loaded_base = None

            for base, edge_names in lang_candidates:
                nf = base / "nodefacts.json"
                if not nf.exists():
                    continue

                # Find the best available edge file for this language dir.
                e = None
                for edge_name in edge_names:
                    candidate = base / edge_name
                    if candidate.exists():
                        e = candidate
                        break
                # Use the preferred name even if it doesn't exist yet
                # (safe_load_json returns {} for missing files).
                if e is None:
                    e = base / edge_names[-1]

                nf_p = nf
                e_p = e
                loaded_base = base
                break

        # ------------------------------------------------------------------
        # Load and normalise
        # ------------------------------------------------------------------
        nf_obj = safe_load_json(nf_p) if nf_p.exists() else {}
        e_obj  = safe_load_json(e_p)  if e_p.exists()  else []

        # nodefacts: accept {"nodes": {...}} or a raw dict/list
        nodes_src = nf_obj.get("nodes") if isinstance(nf_obj, dict) else nf_obj
        nodes = ensure_nodes_dict(nodes_src)

        # edges: accept a bare list or {"edges": [...]}
        if isinstance(e_obj, dict) and isinstance(e_obj.get("edges"), list):
            edges_src = e_obj["edges"]
        else:
            edges_src = e_obj
        edges = edges_list(edges_src)

        log_event(
            "BUILDPIPE:norm_from_canonical_loaded",
            source=str(loaded_base.relative_to(artifacts_dir)) if loaded_base else "root",
            nf=str(nf_p.relative_to(artifacts_dir)) if nf_p.exists() else None,
            edges_path=str(e_p.relative_to(artifacts_dir)) if e_p.exists() else None,
            nodes=len(nodes),
            edges=len(edges),
        )

        return {"meta": {"mode": "classic_only"}, "nodes": nodes, "edges": edges}

    except Exception as e:
        log_event("BUILDPIPE:norm_from_canonical_failed", err=repr(e))
        return None
    
def _ensure_ctx(obj: Any, *, hook_name: str, prior_ctx: BuildContext) -> BuildContext:
    """
    Enforce that pipeline hooks return a BuildContext.

    This is the canonical expectation of run_analyzer_build_pipeline().
    If a hook returns an old/legacy shape (e.g. Path or (art, artifacts_dir)),
    raise with a helpful message rather than letting ctx become a Path.
    """
    if isinstance(obj, BuildContext):
        return obj

    # Very common footgun during refactors: returning a Path.
    if isinstance(obj, Path):
        raise TypeError(
            f"{hook_name} must return BuildContext, got Path: {obj!r}. "
            f"Did you accidentally return workspace_dir/artifacts_dir instead of ctx?"
        )

    # Legacy UI-style init helper sometimes returned (art, artifacts_for_mode).
    # We do NOT support coercing that here because the pipeline needs ctx.
    if isinstance(obj, tuple) and len(obj) == 2:
        raise TypeError(
            f"{hook_name} must return BuildContext, got tuple(len=2): {obj!r}. "
            f"Update the hook to mutate/return ctx (e.g., set ctx.artifacts_dir / ctx.workspace_dir)."
        )

    # Anything else: fail loudly with type info.
    raise TypeError(
        f"{hook_name} must return BuildContext, got {type(obj).__name__}: {obj!r}. "
        f"Update the hook to return ctx."
    )


def _ctx_debug_snapshot(ctx: BuildContext) -> Dict[str, Any]:
    """
    Small, safe snapshot for logs. Avoid dumping huge ctx state.
    """
    d: Dict[str, Any] = {"ctx_type": type(ctx).__name__}
    for k in ("scan_root", "store_root", "workspace_dir", "artifacts_dir", "mode", "output_path", "clean"):
        if hasattr(ctx, k):
            try:
                v = getattr(ctx, k)
                d[k] = str(v) if isinstance(v, Path) else v
            except Exception:
                d[k] = "<err>"
    return d

def run_analyzer_build_pipeline(
    *,
    ctx: BuildContext,
    scan_root: Path,
    store_root: Path,
    allow_output_in_repo: bool,
    output_path: Optional[Path],
    clean: bool,
    cfg_like: Any,
    init_artifacts_ctx: InitArtifactsCtxFn,
    attach_workspace_dir: AttachWorkspaceDirFn,
    follow_up_analyzer: Optional[FollowUpAnalyzerFn],
    run_bucket_analyzers: Optional[RunBucketAnalyzersFn],
    prepare_zones_mode: Optional[PrepareZonesModeFn],
    build_zones: Optional[BuildZonesFn],
    cleanup_temp: Optional[CleanupTempFn],
    run_classic_build: RunClassicBuildFn,
    zones_logger: Optional[ZonesLoggerFn],
) -> Optional[AnalyzerBuildResult]:
    """
    Orchestrates:
      1) init artifacts context (hook; must return BuildContext)
      2) attach workspace (hook; must return BuildContext)
      3) discovery manifest (if present)
      4) classic build (seeded with py files if available)
      5) optional bucket analyzers (manifest-driven)
      6) optional follow-ups (hook + project follow-ups)
      7) optional zones mode prep/build
      8) cleanup temp
      9) return AnalyzerBuildResult

    Key behavior change (multi-language):
      - If discovery_manifest.json exists and contains python files, we feed those
        into ctx.files BEFORE classic build, so classic doesn't run "files=0".
      - We DO NOT let bucket results overwrite canonical/norm unless they are non-empty.
        (Prevents "published_canonical nodes=0 edges=0" from wiping the classic graph.)
    """
    log_event(
        "BUILDPIPE:init_ctx_begin",
        scan_root=str(scan_root),
        store_root=str(store_root),
        allow_output_in_repo=bool(allow_output_in_repo),
        output_path=str(output_path) if output_path else None,
        clean=bool(clean),
    )

    # ---- Init artifacts ctx
    try:
        ctx2 = init_artifacts_ctx(
            ctx=ctx,
            store_root=store_root,
            scan_root=scan_root,
            allow_output_in_repo=allow_output_in_repo,
            output_path=output_path,
            clean=clean,
        )
    except Exception as e:
        log_event("BUILDPIPE:init_artifacts_ctx_call_failed", err=repr(e), **_ctx_debug_snapshot(ctx))
        return None

    if ctx2 is None:
        log_event("BUILDPIPE:init_ctx_failed", **_ctx_debug_snapshot(ctx))
        return None

    try:
        ctx = _ensure_ctx(ctx2, hook_name="init_artifacts_ctx", prior_ctx=ctx)
    except Exception as e:
        log_event("BUILDPIPE:init_ctx_bad_return", err=repr(e))
        return None

    # Defensive: artifacts_dir is required downstream.
    if not hasattr(ctx, "artifacts_dir") or getattr(ctx, "artifacts_dir", None) is None:
        log_event("BUILDPIPE:init_ctx_missing_artifacts_dir", **_ctx_debug_snapshot(ctx))
        return None

    # ---- Attach workspace
    try:
        ctx3 = attach_workspace_dir(ctx=ctx, store_root=store_root)
    except Exception as e:
        log_event("BUILDPIPE:attach_workspace_call_failed", err=repr(e), **_ctx_debug_snapshot(ctx))
        return None

    if ctx3 is None:
        log_event("BUILDPIPE:attach_workspace_failed", **_ctx_debug_snapshot(ctx))
        return None

    try:
        ctx = _ensure_ctx(ctx3, hook_name="attach_workspace_dir", prior_ctx=ctx)
    except Exception as e:
        log_event("BUILDPIPE:attach_workspace_bad_return", err=repr(e))
        return None

    artifacts_dir = cast(Path, ctx.artifacts_dir)

    # ---- Discovery manifest (and seed ctx.files with python files)
    discovery_manifest_path: Optional[Path] = None
    discovery_manifest_summary: Optional[Dict[str, Any]] = None

    py_files_from_manifest: List[Path] = []
    ts_files_from_manifest: List[Path] = []
    go_files_from_manifest: List[Path] = []
    java_files_from_manifest: List[Path] = []
    kotlin_files_from_manifest: List[Path] = []
    rust_files_from_manifest: List[Path] = []
    ruby_files_from_manifest: List[Path] = []

    try:
        mp = artifacts_dir / "discovery_manifest.json"
        if mp.exists():
            discovery_manifest_path = mp
            rows = _load_discovery_manifest(mp) or []

            # Summaries we can log and also return in AnalyzerBuildResult
            discovery_manifest_summary = {"files": len(rows)}

            log_event("BUILDPIPE:manifest_found", path=str(mp), files=len(rows))

            # If manifest rows are dict-like, prefer lang/path; if it's just strings, treat as paths.
            for r in rows:
                if isinstance(r, dict):
                    lang = r.get("lang")
                    rel = r.get("path")
                    if not (isinstance(rel, str) and rel):
                        continue

                    low = rel.lower()
                    p = scan_root / rel

                    if lang == "python" or low.endswith((".py", ".pyi")):
                        py_files_from_manifest.append(p)
                    elif lang in ("ts", "tsx", "typescript") or low.endswith((".ts", ".tsx")):
                        ts_files_from_manifest.append(p)
                    elif lang == "go" or low.endswith(".go"):
                        go_files_from_manifest.append(p)
                    elif lang == "java" or low.endswith(".java"):
                        java_files_from_manifest.append(p)
                    elif lang == "kotlin" or low.endswith((".kt", ".kts")):
                        kotlin_files_from_manifest.append(p)
                    elif lang == "rust" or low.endswith(".rs"):
                        rust_files_from_manifest.append(p)
                    elif lang == "ruby" or low.endswith((".rb", ".rake", ".gemspec", ".ru")):
                        ruby_files_from_manifest.append(p)

                elif isinstance(r, str) and r:
                    low = r.lower()
                    p = scan_root / r
                    if low.endswith((".py", ".pyi")):
                        py_files_from_manifest.append(p)
                    elif low.endswith((".ts", ".tsx")):
                        ts_files_from_manifest.append(p)
                    elif low.endswith(".go"):
                        go_files_from_manifest.append(p)
                    elif low.endswith(".java"):
                        java_files_from_manifest.append(p)
                    elif low.endswith((".kt", ".kts")):
                        kotlin_files_from_manifest.append(p)
                    elif low.endswith(".rs"):
                        rust_files_from_manifest.append(p)
                    elif low.endswith((".rb", ".rake", ".gemspec", ".ru")):
                        ruby_files_from_manifest.append(p)

            # De-dupe deterministically (stable order)
            def _uniq_paths(xs: List[Path]) -> List[Path]:
                seen = set()
                out: List[Path] = []
                for x in xs:
                    k = str(x)
                    if k in seen:
                        continue
                    seen.add(k)
                    out.append(x)
                return out

            py_files_from_manifest = _uniq_paths(py_files_from_manifest)
            ts_files_from_manifest = _uniq_paths(ts_files_from_manifest)
            go_files_from_manifest = _uniq_paths(go_files_from_manifest)
            java_files_from_manifest = _uniq_paths(java_files_from_manifest)
            kotlin_files_from_manifest = _uniq_paths(kotlin_files_from_manifest)
            rust_files_from_manifest = _uniq_paths(rust_files_from_manifest)
            ruby_files_from_manifest = _uniq_paths(ruby_files_from_manifest)

            # Feed python files into classic run (Python-only)
            if py_files_from_manifest:
                try:
                    ctx.files = list(py_files_from_manifest)  # type: ignore[attr-defined]
                except Exception:
                    pass

            # Best-effort: expose language buckets on ctx for downstream hooks
            # (non-breaking: ignore failures if ctx is frozen/typed)
            try:
                setattr(
                    ctx,
                    "files_by_lang",
                    {
                        "python": [p for p in py_files_from_manifest],
                        "ts": [p for p in ts_files_from_manifest],
                        "go": [p for p in go_files_from_manifest],
                        "java": [p for p in java_files_from_manifest],
                        "kotlin": [p for p in kotlin_files_from_manifest],
                        "rust": [p for p in rust_files_from_manifest],
                        "ruby": [p for p in ruby_files_from_manifest],
                    },
                )
            except Exception:
                pass

            log_event(
                "BUILDPIPE:manifest_lang_summary",
                py_files=len(py_files_from_manifest),
                ts_files=len(ts_files_from_manifest),
                go_files=len(go_files_from_manifest),
                java_files=len(java_files_from_manifest),
                kotlin_files=len(kotlin_files_from_manifest),
                rust_files=len(rust_files_from_manifest),
                ruby_files=len(ruby_files_from_manifest),

            )

            log_event(
                "BUILDPIPE:classic_seeded_from_manifest",
                py_files=len(py_files_from_manifest),
            )
        else:
            log_event("BUILDPIPE:manifest_missing", path=str(mp))
    except Exception as e:
        log_event("BUILDPIPE:manifest_err", err=repr(e))

    # ---- Classic build (only if we have python files)
    run_classic = True

    # If we found a manifest and it has no python files, classic would run files=0 and fail.
    if discovery_manifest_path is not None and len(py_files_from_manifest) == 0:
        run_classic = False
        log_event(
            "BUILDPIPE:classic_skipped",
            reason="no_python_files_in_manifest",
            artifacts_dir=str(artifacts_dir),
        )

    art = None
    if run_classic:
        log_event("BUILDPIPE:classic_begin", artifacts_dir=str(artifacts_dir))
        try:
            art = run_classic_build(ctx=ctx)
        except Exception as e:
            log_event("BUILDPIPE:classic_failed", err=repr(e), artifacts_dir=str(artifacts_dir))
            return None
        log_event("BUILDPIPE:classic_done", artifacts_dir=str(artifacts_dir))
    else:
        log_event("BUILDPIPE:classic_done", artifacts_dir=str(artifacts_dir), note="skipped")

    # ---- Bucket analyzers
    # IMPORTANT: buckets are allowed to run, but must not replace norm unless non-empty.
    norm: Optional[Dict[str, Any]] = None
    if run_bucket_analyzers is not None:
        try:
            bucket_norm = run_bucket_analyzers(
                scan_root=scan_root,
                store_root=store_root,
                artifacts_dir=artifacts_dir,
                cfg_like=cfg_like,
                manifest_path=discovery_manifest_path,
            )

            bucket_nodes = 0
            bucket_edges = 0
            if isinstance(bucket_norm, dict):
                n = bucket_norm.get("nodes")
                e = bucket_norm.get("edges")
                if isinstance(n, dict):
                    bucket_nodes = len(n)
                if isinstance(e, list):
                    bucket_edges = len(e)

            # Guardrail: do not allow empty bucket output to wipe canonical.
            if isinstance(bucket_norm, dict) and bucket_nodes > 0:
                norm = bucket_norm
                log_event(
                    "BUILDPIPE:buckets_done",
                    ok=True,
                    nodes=bucket_nodes,
                    edges=bucket_edges,
                )
            else:
                log_event(
                    "BUILDPIPE:buckets_done",
                    ok=False,
                    nodes=bucket_nodes,
                    edges=bucket_edges,
                    note="ignored_empty_bucket_norm",
                )
                norm = None
        except Exception as e:
            log_event("BUILDPIPE:buckets_failed", err=repr(e))
            norm = None
    else:
        log_event("BUILDPIPE:buckets_skipped")

    # ---- Follow-ups
    followups_enabled = follow_up_analyzer is not None

    if followups_enabled:
        try:
            follow_up_analyzer(ctx=ctx)  # optional injected followups
        except Exception as e:
            log_event("BUILDPIPE:followups_hook_failed", err=repr(e))

        try:
            maybe_run_ts_followup(
                scan_root=scan_root,
                store_root=store_root,
                artifacts_dir=artifacts_dir,
                cfg_like=cfg_like,
            )
        except Exception as e:
            log_event("BUILDPIPE:followups_ts_failed", err=repr(e))
    else:
        log_event("BUILDPIPE:followups_skipped")

    # ---- Prepare zones mode
    artifacts_for_mode: Optional[Path] = artifacts_dir

    if prepare_zones_mode is not None:
        log_event("BUILDPIPE:prepare_zones_mode_begin")
        try:
            zdir = prepare_zones_mode(ctx=ctx)
        except Exception as e:
            log_event("BUILDPIPE:prepare_zones_mode_err", err=repr(e))
            zdir = None

        log_event(
            "BUILDPIPE:prepare_zones_mode_done",
            ok=bool(zdir),
            zdir=str(zdir) if zdir is not None else None,
        )

        if zdir is None:
            log_event("BUILDPIPE:prepare_zones_mode_failed")
            return None

        artifacts_for_mode = zdir

    # ---- Build zones
    if build_zones is not None and artifacts_for_mode is not None:
        log_event("BUILDPIPE:zones_build_begin", artifacts_dir=str(artifacts_for_mode))
        try:
            zones_art = build_zones(ctx=ctx, artifacts_dir=artifacts_for_mode)
            if zones_logger is not None:
                try:
                    zones_logger(ctx=ctx, zones_art=zones_art)
                except Exception as e:
                    log_event("BUILDPIPE:zones_logger_failed", err=repr(e))
            log_event("BUILDPIPE:zones_build_done")
        except Exception as e:
            log_event("BUILDPIPE:zones_build_failed", err=repr(e))

    # ---- Cleanup temp
    cleanup_root = artifacts_dir / "_tmp"
    log_event("BUILDPIPE:cleanup_temp_begin", art_root=str(cleanup_root))
    try:
        if cleanup_temp is not None:
            cleanup_temp(tmp_root=cleanup_root)
        else:
            _default_cleanup_tmp(cleanup_root)
    except Exception as e:
        log_event("BUILDPIPE:cleanup_temp_failed", err=repr(e))
    log_event("BUILDPIPE:cleanup_temp_done")

    # ---- Ensure norm (prefer canonical artifacts)
    if norm is None:
        norm = _norm_from_canonical(artifacts_dir)
        if norm is not None:
            log_event(
                "BUILDPIPE:norm_from_canonical_ok",
                nodes=len(norm.get("nodes") or {}),
                edges=len(norm.get("edges") or []),
            )

    log_event(
        "BUILDPIPE:return_result",
        ok=bool(isinstance(norm, dict) and (norm.get("nodes") is not None)),
        artifacts_for_mode=str(artifacts_for_mode) if artifacts_for_mode else None,
    )

    return AnalyzerBuildResult(
        norm=norm,
        artifacts_for_mode=artifacts_for_mode,
        art=art,
        discovery_manifest_path=discovery_manifest_path,
        discovery_manifest_summary=discovery_manifest_summary,
    )
