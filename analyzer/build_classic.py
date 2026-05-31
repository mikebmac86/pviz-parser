# backend/saas_analyzer/analyzer/build_classic.py
from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional
import time as _t
import json
from dataclasses import asdict

from analyzer.config import AnalyzerCfg
from analyzer_store.integration import build_artifacts_and_emit, ensure_cfg
from adapters.analyzer_bridge import enrich_graph
try:
    from diagnostics.logging import log_event
except Exception:  # pragma: no cover
    def log_event(*_a: Any, **_k: Any) -> None:
        return

def _read_json(path: Path, default: Any) -> Any:
    """
    Minimal JSON loader that never raises.
    """
    try:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path: Path, obj: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, sort_keys=False)
    except Exception:
        # best-effort only
        pass


def _merge_nodefacts(dst: dict, src: dict) -> dict:
    dn = dst.get("nodes")
    sn = src.get("nodes")
    if not isinstance(dn, dict) or not isinstance(sn, dict):
        return dst
    for k, v in sn.items():
        if isinstance(k, str) and k not in dn:
            dn[k] = v
    return dst


def _merge_edges(dst: dict, src: dict) -> dict:
    de = dst.get("edges")
    se = src.get("edges")
    if not isinstance(de, list) or not isinstance(se, list):
        return dst

    def _key(e: Any) -> tuple[str, str, str, str]:
        if not isinstance(e, dict):
            return ("", "", "", "")
        s = str(e.get("src") or "")
        d = str(e.get("dst") or "")
        k = str(e.get("kind") or "")
        ev = e.get("evidence") if isinstance(e.get("evidence"), dict) else {}
        spec = str((ev or {}).get("spec_clean") or "")
        return (s, d, k, spec)

    seen = set()
    for e in de:
        seen.add(_key(e))

    for e in se:
        kk = _key(e)
        if kk in seen:
            continue
        seen.add(kk)
        de.append(e)

    return dst


def _merge_folder_index(dst: dict, src: dict) -> dict:
    df = dst.get("files")
    sf = src.get("files")
    if not isinstance(df, dict) or not isinstance(sf, dict):
        return dst

    for file_id, entry in sf.items():
        if not isinstance(file_id, str) or not isinstance(entry, dict):
            continue
        if file_id not in df:
            df[file_id] = entry
            continue

        cur = df[file_id]
        if not isinstance(cur, dict):
            continue

        # additive-only: fill missing fields but don't overwrite python's existing entries
        for key in (
            "imports_all",
            "imports_internal",
            "imports_unresolved",
            "resolved_targets",
            "imports_unresolved_details",
            "imports_resolved_details",
            "imports_details",
        ):
            if key not in cur and key in entry:
                cur[key] = entry[key]

        for key in (
            "imports_all_count",
            "imports_internal_count",
            "imports_unresolved_count",
            "import_style_counts",
            "has_dynamic_imports",
            "has_requires",
            "has_reexports",
        ):
            if key not in cur and key in entry:
                cur[key] = entry[key]

    return dst


def _maybe_run_ts_and_merge(*, scan_root: Path, artifacts_dir: Path, max_bytes: Optional[int]) -> None:
    """
    Best-effort TS analyzer integration:
      - run analyzer.test.analyzer_ts into artifacts_dir/"ts"
      - merge ts artifacts into artifacts_dir root

    Non-fatal by design.
    """
    try:
        from analyzer.test.analyzer_ts import TSAnalyzerCfg, analyze_repo  # type: ignore[import]
    except Exception:
        return

    ts_dir = artifacts_dir / "ts"
    ts_dir.mkdir(parents=True, exist_ok=True)

    try:
        ts_cfg = TSAnalyzerCfg()
        if max_bytes is not None and hasattr(ts_cfg, "max_bytes_per_file"):
            setattr(ts_cfg, "max_bytes_per_file", int(max_bytes))
        analyze_repo(repo_root=scan_root, artifact_dir=ts_dir, cfg=ts_cfg)
    except Exception as e:
        log_event("CLASSIC:ts_analyzer_err", err=repr(e))
        return

    # merge artifacts
    py_nf_p = artifacts_dir / "nodefacts.json"
    py_ed_p = artifacts_dir / "edges.json"
    py_fi_p = artifacts_dir / "folder_index.json"

    ts_nf_p = ts_dir / "nodefacts.json"
    ts_ed_p = ts_dir / "edges.json"
    ts_fi_p = ts_dir / "folder_index.json"

    py_nf = _read_json(py_nf_p, {"schema_version": "nodefacts@v1.6", "nodes": {}}) or {"nodes": {}}
    py_ed = _read_json(py_ed_p, {"schema_version": "edges@v1", "edges": []}) or {"edges": []}
    py_fi = _read_json(py_fi_p, {"schema_version": "folder_index@v1", "files": {}}) or {"files": {}}

    ts_nf = _read_json(ts_nf_p, None)
    ts_ed = _read_json(ts_ed_p, None)
    ts_fi = _read_json(ts_fi_p, None)

    if isinstance(ts_nf, dict):
        py_nf = _merge_nodefacts(py_nf, ts_nf)
    if isinstance(ts_ed, dict):
        py_ed = _merge_edges(py_ed, ts_ed)
    if isinstance(ts_fi, dict):
        py_fi = _merge_folder_index(py_fi, ts_fi)

    _write_json(py_nf_p, py_nf)
    _write_json(py_ed_p, py_ed)
    _write_json(py_fi_p, py_fi)

    # log summary (helpful when validating in real repos)
    try:
        log_event(
            "CLASSIC:ts_merge_done",
            ts_dir=str(ts_dir),
            nodes_added=max(0, len((py_nf.get("nodes") or {})) - len((_read_json(py_nf_p, {}).get("nodes") or {}))),
        )
    except Exception:
        pass

def build_classic(
    *,
    scan_root: Path,
    store_root: Path,
    files: List[Path],
    cfg: Any,
    bus: Any,
    home_id: str,
    artifacts_dir: Optional[Path] = None,
    enable_ts_hook: bool = False,
) -> Optional[dict]:
    """
    Core analyzer classic build (no Qt / no ui.*).

    Returns:
      - A normalized graph dict (nodes/edges/meta/metrics/..)
        or None on failure.

    Notes / fixes vs previous version:
      - Uses the pipeline-provided artifacts_dir when available (prevents split-brain).
      - PASSES artifacts_dir into build_artifacts_and_emit (so it actually writes there).
      - Tolerantly loads edges.json whether it's a list OR {"edges":[...]}.
      - TS/JS "polyglot hook" is disabled by default; BUCKETS owns TS in the new pipeline.
      - Logging edge counts uses list semantics (no {} default).
    """
    log_event(
        "CLASSIC:enter",
        scan_root=str(scan_root),
        store_root=str(store_root),
        files=len(files),
        home_id=home_id,
    )

    cfg_eff: AnalyzerCfg = ensure_cfg(scan_root, cfg)
    log_event("CLASSIC:ensure_cfg_done")

    t0 = _t.perf_counter()
    log_event(
        "CLASSIC:build_begin",
        scan_root=str(scan_root),
        store_root=str(store_root),
        files=len(files),
        home_id=home_id,
    )

    # Canonical artifact dir for this classic run:
    # prefer pipeline-provided artifacts_dir; fallback to store_root/.pviz/artifacts
    art_dir = (
        Path(artifacts_dir) if artifacts_dir is not None else (store_root / ".pviz" / "artifacts")
    ).resolve()
    art_dir.mkdir(parents=True, exist_ok=True)

    log_event("CLASSIC:artifacts_begin", artifacts_dir=str(art_dir))
    res = build_artifacts_and_emit(
        scan_root=str(scan_root.as_posix()),
        store_root=str(store_root.as_posix()),
        files=[p.as_posix() for p in files] if files else None,
        cfg=cfg_eff,
        bus=bus,
        seed_id=home_id,
        direction="classic",
        emit_plan=True,
        artifacts_dir=art_dir,  # CRITICAL: ensure classic writes to the intended dir
    )
    elapsed_ms = (_t.perf_counter() - t0) * 1000.0
    log_event("CLASSIC:artifacts_end", elapsed_ms=elapsed_ms, artifacts_dir=str(art_dir))

    graph_like = (res or {}).get("contracts") if isinstance(res, dict) else None
    has_contracts = bool(graph_like)
    log_event("CLASSIC:contracts_present", present=has_contracts)

    def _load_edges_file(p: Path) -> List[Any]:
        """
        edges.json can be stored as:
          - a list[edge]
          - a dict {"edges": list[edge]}
        Return list[edge] in all cases.
        """
        ed = _read_json(p, None)
        if isinstance(ed, list):
            return ed
        if isinstance(ed, dict) and isinstance(ed.get("edges"), list):
            return ed["edges"]
        return []

    # If contracts are missing, fall back to artifacts we just wrote.
    if not graph_like:
        log_event("CLASSIC:fallback_begin", artifacts_dir=str(art_dir))
        try:
            nf = _read_json(art_dir / "nodefacts.json", {}) or {}
            edges_list = _load_edges_file(art_dir / "edges.json")
            log_event("CLASSIC:fallback_artifacts_used", folder=str(art_dir))
            graph_like = {
                "nodes": nf.get("nodes") or {},
                "edges": edges_list,
            }
        except Exception as e:  # pragma: no cover
            log_event("CLASSIC:fallback_artifacts_err", err=repr(e), artifacts_dir=str(art_dir))
            graph_like = None

    if not isinstance(graph_like, dict) or not graph_like.get("nodes"):
        log_event("CLASSIC:build_failed_no_nodes", elapsed_ms=elapsed_ms, artifacts_dir=str(art_dir))
        log_event("CLASSIC:return_no_nodes", artifacts_dir=str(art_dir))
        return None

    # ---- polyglot hook: run TS analyzer + merge into main artifacts ----
    # Disabled by default. In the new multi-language pipeline, BUCKETS owns TS/JS.
    if enable_ts_hook:
        try:
            max_bytes = getattr(cfg_eff, "max_file_bytes", None)
            _maybe_run_ts_and_merge(
                scan_root=scan_root,
                artifacts_dir=art_dir,
                max_bytes=int(max_bytes) if isinstance(max_bytes, int) else None,
            )
        except Exception as e:
            log_event("CLASSIC:ts_hook_err", err=repr(e), artifacts_dir=str(art_dir))

        # If we merged artifacts, align returned graph with merged artifacts so callers see it.
        try:
            nf2 = _read_json(art_dir / "nodefacts.json", None)
            if isinstance(nf2, dict) and isinstance(nf2.get("nodes"), dict):
                graph_like["nodes"] = dict(graph_like.get("nodes") or {})
                for k, v in nf2["nodes"].items():
                    if k not in graph_like["nodes"]:
                        graph_like["nodes"][k] = v

            edges_list2 = _load_edges_file(art_dir / "edges.json")
            if edges_list2:
                graph_like["edges"] = edges_list2
        except Exception:
            pass

    try:
        n_nodes = len(graph_like.get("nodes") or {})
        edges_val = graph_like.get("edges") or []
        n_edges = len(edges_val) if isinstance(edges_val, list) else 0
        log_event("CLASSIC:graph_summary", nodes=n_nodes, edges=n_edges, elapsed_ms=elapsed_ms)
    except Exception:
        log_event("CLASSIC:graph_summary_err")

    norm: dict = dict(graph_like)
    norm.setdefault("_contracts_format", "normalized")

    # Normalize edges to list shape (defensive)
    if not isinstance(norm.get("edges"), list):
        norm["edges"] = []

    log_event("CLASSIC:enrich_begin")
    try:
        enriched = enrich_graph(norm) or {}
        if isinstance(enriched.get("metrics"), dict):
            m = norm.setdefault("metrics", {})
            if isinstance(m, dict):
                m.update(enriched["metrics"])
        if isinstance(enriched.get("meta"), dict):
            mm = norm.setdefault("meta", {})
            if isinstance(mm, dict):
                for k, v in enriched["meta"].items():
                    mm.setdefault(k, v)
        log_event("CLASSIC:enrich_end")
    except Exception as e:  # pragma: no cover
        log_event("CLASSIC:enrich_err", err=repr(e))

    norm["analyzer_cfg"] = asdict(cfg_eff)
    log_event("CLASSIC:analyzer_cfg_attached")

    if not norm.get("nodes"):
        log_event("CLASSIC:build_empty_after_enrich")
        log_event("CLASSIC:return_empty_after_enrich")
        return None

    log_event(
        "CLASSIC:build_end_ok",
        nodes=len(norm.get("nodes") or {}),
        edges=len(norm.get("edges") or []),
    )
    log_event(
        "CLASSIC:return",
        nodes=len(norm.get("nodes") or {}),
        edges=len(norm.get("edges") or []),
    )
    return norm
