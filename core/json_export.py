# core/json_export.py
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
import json
import shutil
from diagnostics.logging import log_event
from analyzer.discovery_manifest import ensure_discovery_manifest
from analyzer.config import AnalyzerCfg
from core.build_context import BuildContext, make_build_context
from core.build_pipeline import (
    AnalyzerBuildResult,
    run_analyzer_build_pipeline,
    run_bucket_analyzers_default,
)
from core.artifacts_headless import (
    init_artifacts_ctx_headless,
    cleanup_temp_headless,
)
from i_o.export_llm_json import (
    LLMExportConfig,
    export_llm_bundle,
    LLMJsonExportError,
)
from core.build_pipeline.buckets import merge_folder_indexes
# ---------------------------------------------------------------------------
# Simple headless "bus" (can be replaced with your real event bus)
# ---------------------------------------------------------------------------


class NullBus:
    def emit(self, *_a: Any, **_k: Any) -> None:
        pass


def _make_headless_ctx(
    *,
    scan_root: Path,
    store_root: Path,
    artifacts_dir: Path,
    cfg: AnalyzerCfg,
    files: Sequence[Path],
    home_id: str,
    mode: str = "zones",
    eff_mode: Optional[str] = None,
) -> BuildContext:
    """
    Construct a UI-free BuildContext suitable for the shared build pipeline.
    """
    bus = NullBus()
    return make_build_context(
        scan_root=scan_root,
        store_root=store_root,
        artifacts_dir=artifacts_dir,
        cfg=cfg,
        mode=mode,
        eff_mode=eff_mode,
        files=list(files),
        home_id=home_id,
        bus=bus,
    )


# ---------------------------------------------------------------------------
# Headless adapters for core.build_pipeline
# ---------------------------------------------------------------------------


def _headless_init_artifacts_ctx(
    *,
    ctx: BuildContext,
    store_root: Path,
    scan_root: Path,
    allow_output_in_repo: bool = False,
    output_path: Optional[Path] = None,
    clean: bool = False,
    **_kw: Any,
) -> BuildContext:
    """
    build_pipeline hook: initialize headless artifacts and return updated ctx.
    """
    mode = (
        getattr(ctx, "mode", None)
        or getattr(ctx, "build_mode", None)
        or getattr(ctx, "graph_mode", None)
    )
    if not mode:
        raise RuntimeError("headless init_artifacts_ctx: ctx is missing 'mode'")

    artifacts_dir = getattr(ctx, "artifacts_dir", None)
    if artifacts_dir is None:
        artifacts_dir = Path(store_root) / ".pviz" / "artifacts"

    art, artifacts_for_mode = init_artifacts_ctx_headless(
        store_root=store_root,
        artifacts_dir=Path(artifacts_dir),
        mode=str(mode),
    )

    if artifacts_for_mode is None:
        artifacts_for_mode = Path(artifacts_dir)

    ctx.artifacts_dir = Path(artifacts_for_mode)

    # Mirror common ctx fields if present.
    if hasattr(ctx, "scan_root"):
        ctx.scan_root = scan_root
    if hasattr(ctx, "store_root"):
        ctx.store_root = store_root
    if hasattr(ctx, "allow_output_in_repo"):
        ctx.allow_output_in_repo = allow_output_in_repo
    if hasattr(ctx, "output_path"):
        ctx.output_path = output_path
    if hasattr(ctx, "clean"):
        ctx.clean = clean

    if hasattr(ctx, "art"):
        try:
            ctx.art = art
        except Exception:
            pass

    return ctx


def _headless_attach_workspace_dir(
    *,
    ctx: BuildContext,
    store_root: Optional[Path] = None,
    **_kw: Any,
) -> BuildContext:
    """
    build_pipeline hook: attach workspace dir to ctx and return ctx.

    Headless convention: workspace dir = <store_root>/.pviz
    """
    if store_root is None:
        store_root = getattr(ctx, "store_root", None)

    ws = getattr(ctx, "workspace_dir", None) or getattr(ctx, "workspace_path", None)
    if ws is None and store_root is not None:
        ws = Path(store_root) / ".pviz"

    if hasattr(ctx, "workspace_dir"):
        try:
            ctx.workspace_dir = Path(ws) if ws is not None else None
        except Exception:
            pass

    return ctx


def _headless_prepare_zones_mode(*, ctx: BuildContext) -> Optional[Path]:
    """
    build_pipeline hook: prepare zones mode and return the artifacts dir to use.
    """
    try:
        from core.zones_headless import prepare_zones_mode_headless
    except Exception:  # pragma: no cover
        return getattr(ctx, "artifacts_dir", None)

    scan_root = Path(getattr(ctx, "scan_root"))
    store_root = Path(getattr(ctx, "store_root"))
    artifacts_dir = Path(getattr(ctx, "artifacts_dir"))
    cfg_like = getattr(ctx, "cfg", None)
    bus = getattr(ctx, "bus", None)
    art = getattr(ctx, "art", None)

    try:
        zdir = prepare_zones_mode_headless(
            scan_root=scan_root,
            store_root=store_root,
            artifacts_dir=artifacts_dir,
            cfg_like=cfg_like,
            bus=bus,
            art=art,
        )
        return zdir or artifacts_dir
    except Exception:
        return artifacts_dir


def _headless_cleanup_temp(*, tmp_root: Path) -> None:
    """
    build_pipeline hook: cleanup tmp root.
    """
    try:
        cleanup_temp_headless(tmp_root.parent)
        return
    except Exception:
        pass

    try:
        if tmp_root.exists():
            shutil.rmtree(tmp_root, ignore_errors=True)
    except Exception:
        pass


def _write_canonical_from_norm(*, artifacts_dir: Path, norm: Dict[str, Any]) -> None:
    """
    Persist canonical artifacts from a normalized graph dict.

    CRITICAL: write BOTH nodefacts.json and edges.json when present.
    """
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    nodes = norm.get("nodes") or {}
    (artifacts_dir / "nodefacts.json").write_text(
        json.dumps({"nodes": nodes}, indent=2),
        encoding="utf-8",
    )

    if "edges" in norm:
        edges = norm.get("edges") or []
        (artifacts_dir / "edges.json").write_text(
            json.dumps(edges, indent=2),
            encoding="utf-8",
        )

def _headless_run_classic(*, ctx: BuildContext) -> Dict[str, Any]:
    """
    build_pipeline hook: run the classic analyzer and return a normalized graph dict.

    Path policy:
      - Classic writes into a step-specific set dir: <artifacts_root>/sets/classic/
      - Classic MUST NOT publish canonical artifacts to <artifacts_root>/ (that is done later)
    """
    try:
        from analyzer.build_classic import build_classic  # your actual entrypoint
    except Exception as e:  # pragma: no cover
        raise RuntimeError(f"classic analyzer not available: {e}") from e

    scan_root = Path(getattr(ctx, "scan_root"))
    store_root = Path(getattr(ctx, "store_root"))
    artifacts_root = Path(getattr(ctx, "artifacts_dir"))  # canonical root (NOT where classic writes)
    cfg_like = getattr(ctx, "cfg", None)
    bus = getattr(ctx, "bus", None)
    home_id = getattr(ctx, "home_id", None)

    # Classic output set dir
    classic_dir = artifacts_root / "sets" / "classic"
    classic_dir.mkdir(parents=True, exist_ok=True)

    files_seq = getattr(ctx, "files", None) or []
    files: List[Path] = []
    for f in files_seq:
        files.append(f if isinstance(f, Path) else Path(str(f)))

    # IMPORTANT: build_classic must accept artifacts_dir and forward it to build_artifacts_and_emit()
    norm = build_classic(
        scan_root=scan_root,
        store_root=store_root,
        artifacts_dir=classic_dir,
        files=files,
        cfg=cfg_like,
        bus=bus,
        home_id=home_id,
    )

    if not isinstance(norm, dict):
        raise RuntimeError("classic build did not return a normalized graph dict")

    # Do NOT publish canonical here. Canonicalization/merge happens later.
    return norm

def _headless_build_zones(*, ctx: BuildContext, artifacts_dir: Path) -> Optional[dict]:
    """
    build_pipeline hook: zones build.

    Contract:
      - READ canonical nodefacts+edges from <artifacts_root>/ (ctx.artifacts_dir)
      - WRITE zones artifacts into the provided artifacts_dir (zones output set dir)
    """
    artifacts_root = Path(getattr(ctx, "artifacts_dir"))  # canonical root
    zones_out_dir = Path(artifacts_dir)                   # zones output dir (set)
    zones_out_dir.mkdir(parents=True, exist_ok=True)

    # Load canonical nodefacts+edges from artifacts_root
    norm: Optional[Dict[str, Any]] = None
    try:
        nf_p = artifacts_root / "nodefacts.json"
        e_p = artifacts_root / "edges.json"

        if nf_p.exists() and e_p.exists():
            nf = json.loads(nf_p.read_text(encoding="utf-8"))
            ed = json.loads(e_p.read_text(encoding="utf-8"))

            nodes = nf.get("nodes") if isinstance(nf, dict) else None
            edges = ed if isinstance(ed, list) else (ed.get("edges") if isinstance(ed, dict) else None)

            if isinstance(nodes, dict) and isinstance(edges, list):
                norm = {"meta": {"mode": "canonical"}, "nodes": nodes, "edges": edges}
    except Exception:
        norm = None

    if not isinstance(norm, dict):
        return None

    try:
        from core.pkgzones.builder import build_package_zones, zones_to_artifact
    except Exception:  # pragma: no cover
        return None

    home_id = getattr(ctx, "home_id", None)
    if not isinstance(home_id, str) or not home_id:
        return None

    try:
        zones, _seen = build_package_zones(graph=norm, seed=home_id, cfg=None)
        artifact = zones_to_artifact(zones, graph_meta=(norm.get("meta") or {}))

        out_path = zones_out_dir / "zones_by_package@v1.json"
        out_path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")

        return artifact
    except Exception:
        return None


def _headless_zones_logger(*, ctx: BuildContext, zones_art: Any) -> None:
    """
    build_pipeline zones_logger hook: receives ctx + zones_art.
    """
    try:
        from diagnostics.logging import log_event as _log_event
    except Exception:  # pragma: no cover
        return

    try:
        size = None
        if isinstance(zones_art, dict):
            z = zones_art.get("zones")
            if isinstance(z, list):
                size = len(z)
        _log_event("HEADLESS:zones_built", zones_count=size)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public headless API: build + JSON export
# ---------------------------------------------------------------------------


def build_llm_json_headless(
    *,
    scan_root: Path,
    store_root: Path,
    artifacts_dir: Path,
    cfg: AnalyzerCfg,
    files: Sequence[Path],
    home_id: str,
    json_output: Path,
    mode: str = "zones",
    eff_mode: Optional[str] = None,
) -> tuple[Path, AnalyzerBuildResult]:
    """
    Headless entry to build analyzer artifacts and dump the normalized graph
    to a JSON file suitable for LLM consumption.
    """
    ctx = _make_headless_ctx(
        scan_root=scan_root,
        store_root=store_root,
        artifacts_dir=artifacts_dir,
        cfg=cfg,
        files=files,
        home_id=home_id,
        mode=mode,
        eff_mode=eff_mode,
    )

    result = run_analyzer_build_pipeline(
        ctx=ctx,
        scan_root=scan_root,
        store_root=store_root,
        allow_output_in_repo=False,
        output_path=json_output,
        clean=False,
        cfg_like=cfg,
        init_artifacts_ctx=_headless_init_artifacts_ctx,
        attach_workspace_dir=_headless_attach_workspace_dir,
        follow_up_analyzer=None,
        run_bucket_analyzers=None,
        prepare_zones_mode=_headless_prepare_zones_mode if mode == "zones" else None,
        build_zones=_headless_build_zones if mode == "zones" else None,
        cleanup_temp=_headless_cleanup_temp,
        run_classic_build=_headless_run_classic,
        zones_logger=_headless_zones_logger,
    )

    if result is None or not isinstance(result.norm, dict):
        raise RuntimeError("Headless analyzer build failed or produced no graph")

    json_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(json.dumps(result.norm, indent=2), encoding="utf-8")

    return json_output, result

def build_llm_bundle_headless(
    *,
    scan_root: Path,
    store_root: Path,
    cfg: AnalyzerCfg,
    files: Sequence[Path],
    home_id: str,
    bundle_output: Path,
    mode: str = "zones",
    eff_mode: Optional[str] = None,
    use_bucket_analyzers: bool = True,
) -> tuple[Path, Optional[Path], AnalyzerBuildResult]:
    """
    Headless entry to:
      1) Run the standard analyzer pipeline into the sandbox.
      2) Publish merged canonical artifacts from per-language artifact sets.
      3) Emit an LLM bundle JSON using canonical artifacts.

    Returns:
        (standard_path, compressed_path, result)
        compressed_path is None if compression failed
    """
    artifacts_root = store_root / ".pviz" / "artifacts"

    # Ensure discovery manifest exists so BUCKETS can dispatch.
    try:
        manifest_path, _summary = ensure_discovery_manifest(
            scan_root=scan_root,
            artifacts_dir=artifacts_root,
            force=False,
        )

        compat_manifest = artifacts_root / "discovery_manifest.json"
        if not compat_manifest.exists():
            try:
                compat_manifest.write_text(
                    manifest_path.read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
            except Exception:
                pass
    except Exception:
        pass

    ctx = _make_headless_ctx(
        scan_root=scan_root,
        store_root=store_root,
        artifacts_dir=artifacts_root,
        cfg=cfg,
        files=files,
        home_id=home_id,
        mode=mode,
        eff_mode=eff_mode,
    )

    result = run_analyzer_build_pipeline(
        ctx=ctx,
        scan_root=scan_root,
        store_root=store_root,
        allow_output_in_repo=False,
        output_path=bundle_output,
        clean=False,
        cfg_like=cfg,
        init_artifacts_ctx=_headless_init_artifacts_ctx,
        attach_workspace_dir=_headless_attach_workspace_dir,
        follow_up_analyzer=None,
        run_bucket_analyzers=(run_bucket_analyzers_default if use_bucket_analyzers else None),
        prepare_zones_mode=_headless_prepare_zones_mode if mode == "zones" else None,
        build_zones=_headless_build_zones if mode == "zones" else None,
        cleanup_temp=_headless_cleanup_temp,
        run_classic_build=_headless_run_classic,
        zones_logger=_headless_zones_logger,
    )

    if result is None or not isinstance(result.norm, dict):
        raise RuntimeError("Headless analyzer build failed or produced no graph")

    # ------------------------------------------------------------------
    # This is the single point where polyglot is merged for zones/export.
    # ------------------------------------------------------------------
    try:
        from core.artifact_sets_merge import publish_canonical_from_sets

        python_set_dir = artifacts_root / "sets" / "classic"
        ts_set_dir = artifacts_root / "sets" / "analyzers" / "ts"

        merge_summary = publish_canonical_from_sets(
            artifacts_root=artifacts_root,
            python_set_dir=python_set_dir,
            ts_set_dir=ts_set_dir,
            out_dir=artifacts_root,  # canonical root for exporter + zones
            merge_folder_indexes_fn=merge_folder_indexes,  # if in scope; else omit
        )
        log_event("CANON:published_from_sets", **(merge_summary.get("counts") or {}))
    except Exception as e:
        # Best-effort: exporter can still run with python-only canonical
        log_event("CANON:publish_from_sets_failed", err=repr(e))

    repo_root = scan_root
    repo_name = repo_root.name

    # Generate standard format
    cfg_llm_standard = LLMExportConfig(
        artifact_dir=Path(artifacts_root),
        output_path=bundle_output,
        repo_root=repo_root,
        repo_name=repo_name,
        mode=mode,
        output_format="standard",
    )

    try:
        export_llm_bundle(cfg_llm_standard)
    except LLMJsonExportError as e:
        raise RuntimeError(f"LLM bundle export (standard) failed: {e}") from e

    # Generate compressed format
    compressed_path = bundle_output.parent / f"{bundle_output.stem}.compressed{bundle_output.suffix}"
    cfg_llm_compressed = LLMExportConfig(
        artifact_dir=Path(artifacts_root),
        output_path=compressed_path,
        repo_root=repo_root,
        repo_name=repo_name,
        mode=mode,
        output_format="compressed",
    )

    try:
        export_llm_bundle(cfg_llm_compressed)
        log_event(
            "EXPORT:dual_format_generated",
            standard_size=bundle_output.stat().st_size,
            compressed_size=compressed_path.stat().st_size,
            compression_ratio=f"{(1 - compressed_path.stat().st_size / bundle_output.stat().st_size) * 100:.0f}%"
        )
    except Exception as e:
        # Log but don't fail - standard format is still available
        log_event("EXPORT:compressed_generation_failed", error=str(e))
        compressed_path = None

    return bundle_output, compressed_path, result
