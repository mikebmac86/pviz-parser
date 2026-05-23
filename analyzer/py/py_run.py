# backend/saas_analyzer/analyzer/py/run.py
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Sequence

from analyzer.config import AnalyzerCfg  # your existing python cfg model

def analyze_files_py(
    *,
    repo_root: Path,
    artifact_dir: Path,
    cfg: AnalyzerCfg,
    files: Sequence[Path],
    store_root: Path | None = None,
    bus: Any = None,
    home_id: str | None = None,
) -> Dict[str, object]:
    """
    Bucket/file-list adapter for the existing Python analyzer.

    Goal:
      - Accept a file list (bucket contract)
      - Reuse the existing python analyzer (classic build)
      - Produce a fragment dict for the bucket merger
      - Ensure artifacts exist under artifact_dir (Option B expectation)

    Key behavior:
      - build_classic writes its canonical artifacts under:
            <store_root>/.pviz/artifacts/
      - Option B expects per-language artifacts under:
            artifact_dir  (e.g., .../.pviz/artifacts/analyzers/python)
      - Therefore we run build_classic, then mirror/copy the canonical artifacts
        into artifact_dir.

    """
    import shutil

    repo_root = repo_root.resolve()
    artifact_dir = artifact_dir.resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)

    from core.store_root import default_store_root
    sr = store_root.resolve() if store_root else default_store_root()

    # Convert to Paths (and ensure they're inside repo_root)
    safe_files: list[Path] = []
    for p in files:
        try:
            ap = p.resolve()
        except Exception:
            continue
        if not ap.exists() or not ap.is_file():
            continue
        try:
            ap.relative_to(repo_root)
        except Exception:
            continue
        safe_files.append(ap)

    # Reuse existing classic python analyzer
    from analyzer.build_classic import build_classic  # type: ignore[import]

    norm = build_classic(
        scan_root=repo_root,
        store_root=sr,
        files=safe_files,
        cfg=cfg,
        bus=bus,
        home_id=home_id or repo_root.name,
    )

    # ---- Mirror canonical artifacts into artifact_dir (Option B contract) ----
    # Canonical location produced by the python pipeline
    canonical_art_dir = sr / ".pviz" / "artifacts"

    # Prefer names from cfg if present; fall back to canonical defaults.
    nodefacts_name = getattr(cfg, "nodefacts_name", "nodefacts.json")
    edges_name = getattr(cfg, "edges_name", "edges.json")
    folder_index_name = getattr(cfg, "folder_index_name", "folder_index.json")
    plan_name = getattr(cfg, "plan_name", "plan.json")  # optional in some setups

    mirrored: dict[str, bool] = {}
    for name in (nodefacts_name, edges_name, folder_index_name, plan_name):
        src = canonical_art_dir / name
        dst = artifact_dir / name
        try:
            if src.exists() and src.is_file():
                shutil.copy2(src, dst)
                mirrored[name] = True
            else:
                mirrored[name] = False
        except Exception:
            mirrored[name] = False

    # Defensive: return a fragment even if build_classic returns a different shape
    nodes: list[Any] = []
    edges: list[Any] = []
    if isinstance(norm, dict):
        n = norm.get("nodes")
        e = norm.get("edges")
        if isinstance(n, list):
            nodes = n
        if isinstance(e, list):
            edges = e

    return {
        "nodes": nodes,
        "edges": edges,
        "meta": {
            "analyzer": "python",
            "files_in": len(files),
            "files_used": len(safe_files),
            "artifact_dir": str(artifact_dir),
            "store_root": str(sr),
            "canonical_artifact_dir": str(canonical_art_dir),
            "mirrored": mirrored,
            "note": "bucket adapter around existing build_classic; mirrors canonical artifacts into per-language dir",
        },
    }
