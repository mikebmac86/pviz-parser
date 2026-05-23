# diagnostics/events.py
from __future__ import annotations
from typing import TypedDict, Literal, NotRequired, Optional, Dict, List

# ---------------------------------------------------------------------------
# Analyzer artifacts built
# ---------------------------------------------------------------------------

ANALYZER_ARTIFACTS_BUILT = "analyzer_store:artifacts_built"

class CoverageRatio(TypedDict):
    numer: int
    denom: int
    ratio: float

class CoverageSummary(TypedDict):
    by_count: CoverageRatio
    by_edges: CoverageRatio

class ArtifactsPaths(TypedDict, total=False):
    folder_index: str
    nodefacts: str
    coverage: NotRequired[Optional[str]]
    reachable: NotRequired[Optional[str]]
    plan: NotRequired[Optional[str]]
    layout: NotRequired[Optional[str]]
    edges: NotRequired[Optional[str]]
    diagnostics: NotRequired[Optional[str]]

class LayoutOverview(TypedDict, total=False):
    seed: Optional[str]
    mode: str                   # e.g., "bfs-gen-seq"
    levels: int
    nodes: int
    plan_path: Optional[str]
    layout_path: Optional[str]
    edges_path: Optional[str]
    diagnostics_path: Optional[str]

class AnalyzerArtifactsBuilt(TypedDict, total=False):
    kind: Literal["analyzer_store:artifacts_built"]
    meta: Dict[str, Optional[str]]        # {root, seed, created}
    paths: ArtifactsPaths                  # includes reachable/plan/layout/edges/diagnostics (optional)
    counts: Dict[str, int]                 # {eligible, parsed, reachable}
    edges: Dict[str, int]                  # {internal_total, reachable_total}
    scc: Dict[str, int]                    # {count, largest}
    coverage: CoverageSummary
    top_uncovered: List[str]
    parse_issues: List[str]                # now simple strings ("file: ErrorType")
    external_roots: List[str]
    layout: LayoutOverview                 # extra convenience block for UI

