# core/zones_headless.py
from __future__ import annotations

from pathlib import Path
from typing import Optional, Any

from diagnostics.logging import log_event as _log_event
from core.zones_ctx import (
    resolve_zones_artifacts_dir_core,
    build_zones_context,
    discovery_graph_path,
)


def _zlog(event: str, **fields: Any) -> None:
    _log_event(f"ZONES:{event}", **fields)


def prepare_zones_mode_headless(
    *,
    scan_root: Path,
    store_root: Path,
    artifacts_dir: Optional[Path],
    cfg_like: object,
    bus: object,
    art: object,
    zone_kind: str = "by-package",
) -> Optional[Path]:
    """
    Headless counterpart of the GUI zones-mode preparation.

    Responsibilities:
      - Resolve the effective zones artifact directory (zdir).
      - Point the artifact context at zdir (art.root = zdir).
      - Build a zones context for membership / diagnostics.

    Discovery graph building is a UI-only concern and is omitted here.
    """
    _zlog("mode_zones_enter")

    try:
        if artifacts_dir is None:
            artifacts_dir = store_root / ".pviz" / "artifacts" / "zones"

        zdir = resolve_zones_artifacts_dir_core(
            store_root=store_root,
            fallback=artifacts_dir,
            prior_ctx=None,
            window_dir=None,
        )

        try:
            art.root = zdir  # type: ignore[attr-defined]
        except Exception as e:
            _zlog("art_root_set_err", err=repr(e))

        try:
            build_zones_context(zdir)
        except Exception as e:
            _zlog("zones_ctx_headless_err", err=repr(e))

        d_graph = discovery_graph_path(zdir)
        _zlog("discovery_check", path=str(d_graph), exists=d_graph.exists())
        _zlog("zones_artifacts_dir", dir=str(zdir))

    except Exception as e:
        _zlog("zones_artifacts_dir_err", err=repr(e))
        _log_event("BUILD:zones_artifacts_dir_err", err=repr(e))
        return None

    return zdir