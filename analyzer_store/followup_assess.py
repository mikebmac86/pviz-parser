from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Set

from analyzer_store.folder_index import load_folder_index  # wherever yours lives
from analyzer_store.coverage import load_coverage


@dataclass(frozen=True)
class FollowupAssessment:
    lang: str
    eligible_nodes: int
    covered_nodes: int
    pending_nodes: int
    should_run: bool


def assess_followup(
    *,
    artifacts_dir: Path,
    scan_root: Path,
    lang: str,
    folder_index_path: Optional[Path] = None,
) -> FollowupAssessment:
    # Load folder index
    if folder_index_path is None:
        folder_index_path = artifacts_dir / "folder_index.json"

    idx = load_folder_index(folder_index_path)

    eligible: Set[str] = {
        mod_id
        for mod_id, fe in idx.files.items()
        if getattr(fe, "eligible", True)
    }

    cov = load_coverage(artifacts_dir=artifacts_dir, lang=lang)
    covered = {k for k, v in cov.covered_nodes.items() if v}

    pending = eligible - covered

    return FollowupAssessment(
        lang=lang,
        eligible_nodes=len(eligible),
        covered_nodes=len(eligible & covered),
        pending_nodes=len(pending),
        should_run=len(pending) > 0,
    )
