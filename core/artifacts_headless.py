# core/artifacts_headless.py
from __future__ import annotations

from pathlib import Path
from typing import Optional, Any

from diagnostics.logging import log_event
from .artifacts import make_ctx as make_art_ctx, prune_temp_files


def _log(kind: str, **fields: Any) -> None:
    # Best-effort only; never let logging break builds.
    try:
        log_event(kind, **fields)
    except Exception:
        return


def init_artifacts_ctx_headless(
    store_root: Optional[Path],
    artifacts_dir: Optional[Path],
    mode: Optional[str],
) -> tuple[object, Optional[Path]]:
    """
    Headless adapter for build_pipeline InitArtifactsCtxFn.

    Canonical artifacts behavior lives in core.artifacts (make_ctx/path_for/save/load).
    """
    if store_root is None:
        raise RuntimeError("init_artifacts_ctx_headless: store_root is None")

    eff_mode_for_art = "zones" if mode == "zones" else "classic"
    art = make_art_ctx(store_root, mode=eff_mode_for_art)

    # Log what we decided; avoid extra deps here.
    _log("BUILD:art_ctx_headless", root=str(getattr(art, "root", "")), mode=eff_mode_for_art)

    # Preserve existing contract: return artifacts_dir unchanged as artifacts_for_mode.
    return art, artifacts_dir


def cleanup_temp_headless(root: Path) -> None:
    """
    Headless cleanup adapter (best-effort).
    """
    try:
        prune_temp_files(root)
        _log("BUILD:cleanup_headless", root=str(root))
    except Exception as e:
        _log("BUILD:cleanup_headless_err", root=str(root), err=repr(e))
