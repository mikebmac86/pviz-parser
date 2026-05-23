from __future__ import annotations

from pathlib import Path
from typing import Any
try:
    from diagnostics.logging import log_event
except Exception:  # pragma: no cover
    def log_event(*_a: Any, **_k: Any) -> None:
        return


def maybe_run_python_followup(
    *,
    scan_root: Path,
    store_root: Path,
    artifacts_dir: Path,
    cfg: Any,
    bus: Any,
    home_id: str,
) -> None:
    """
    Best-effort python follow-up that attempts to cover missing files in nodefacts by
    generating nodefacts for pending files and merging into canonical nodefacts.
    """
    try:
        from analyzer_store.folder_index import load_folder_index
        from analyzer_store.nodefacts import load_nodefacts, build_nodefacts
        # Keep this import as-is for compatibility with the existing project layout.
        from analyzer.build_classic import _merge_nodefacts  # type: ignore
    except Exception as e:
        log_event("FOLLOWUP:python_import_failed", err=repr(e))
        return

    try:
        idx = load_folder_index(artifacts_dir / "folder_index.json")
        nf = load_nodefacts(artifacts_dir / "nodefacts.json")
    except Exception as e:
        log_event("FOLLOWUP:python_load_failed", err=repr(e))
        return

    try:
        reachable_ids = set(getattr(nf, "nodes", {}).keys())
        all_ids = set(getattr(idx, "files", {}).keys())
        pending = sorted(all_ids - reachable_ids)
    except Exception as e:
        log_event("FOLLOWUP:python_index_err", err=repr(e))
        return

    log_event(
        "FOLLOWUP:python_candidates",
        total=len(all_ids),
        reachable=len(reachable_ids),
        pending=len(pending),
    )

    if not pending:
        return

    try:
        extra_nf = build_nodefacts(
            seed_ids=set(pending),
            idx=idx,
            cfg=cfg,
            store_root=store_root,
            bus=bus,
            home_id=home_id,
        )
    except Exception as e:
        log_event("FOLLOWUP:python_build_failed", err=repr(e))
        return

    try:
        _merge_nodefacts(
            artifacts_dir=artifacts_dir,
            base_nf=nf,
            extra_nf=extra_nf,
        )
        log_event("FOLLOWUP:python_merge_ok", extra_nodes=len(getattr(extra_nf, "nodes", {}) or {}))
    except Exception as e:
        log_event("FOLLOWUP:python_merge_failed", err=repr(e))


def maybe_run_ts_followup(
    *,
    scan_root: Path,
    store_root: Path,
    artifacts_dir: Path,
    cfg_like: Any,
) -> None:
    """
    Best-effort TS follow-up: runs TS analyzer and merges into python artifacts if wired.
    """
    try:
        from analyzer.ts.followup import _maybe_run_ts_and_merge  # type: ignore
    except Exception as e:
        log_event("FOLLOWUP:ts_import_failed", err=repr(e))
        return

    try:
        _maybe_run_ts_and_merge(
            scan_root=scan_root,
            store_root=store_root,
            artifacts_dir=artifacts_dir,
            cfg_like=cfg_like,
        )
        log_event("FOLLOWUP:ts_ok")
    except Exception as e:
        log_event("FOLLOWUP:ts_failed", err=repr(e))
