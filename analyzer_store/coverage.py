from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping

from analyzer_store.io_utils import iso_utc, atomic_write_json, load_json_bytes


@dataclass(frozen=True)
class Coverage:
    schema: str
    meta: Mapping[str, str]
    covered_nodes: Mapping[str, bool]


def _cov_path(artifacts_dir: Path, *, lang: str) -> Path:
    # versioned per language follow-up
    return artifacts_dir / f"coverage_{lang}@v1.json"


def load_coverage(*, artifacts_dir: Path, lang: str) -> Coverage:
    path = _cov_path(artifacts_dir, lang=lang)
    if not path.exists():
        return Coverage(
            schema=f"coverage_{lang}@v1",
            meta={"created": iso_utc(), "root": ""},
            covered_nodes={},
        )
    data = load_json_bytes(path)
    if not isinstance(data, dict):
        raise ValueError(f"Invalid coverage JSON at {path}")
    schema = data.get("schema")
    want = f"coverage_{lang}@v1"
    if schema != want:
        raise ValueError(f"Unexpected schema: {schema} (want {want})")
    return Coverage(
        schema=want,
        meta=dict(data.get("meta") or {}),
        covered_nodes=dict(data.get("covered_nodes") or {}),
    )


def save_coverage(*, artifacts_dir: Path, lang: str, cov: Coverage) -> None:
    path = _cov_path(artifacts_dir, lang=lang)
    payload = {
        "schema": cov.schema,
        "meta": dict(cov.meta),
        "covered_nodes": dict(cov.covered_nodes),
    }
    atomic_write_json(payload, path)


def update_coverage_from_norm(*, artifacts_dir: Path, scan_root: Path, lang: str, norm: dict) -> None:
    nodes = norm.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        return

    cov = load_coverage(artifacts_dir=artifacts_dir, lang=lang)
    merged: Dict[str, bool] = dict(cov.covered_nodes)

    for node_id in nodes.keys():
        merged[str(node_id)] = True

    save_coverage(
        artifacts_dir=artifacts_dir,
        lang=lang,
        cov=Coverage(
            schema=f"coverage_{lang}@v1",
            meta={"created": iso_utc(), "root": str(scan_root.resolve())},
            covered_nodes=merged,
        ),
    )
