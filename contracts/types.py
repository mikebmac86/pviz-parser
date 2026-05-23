from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import (
    Any,
    Literal,
    NewType,
    TypedDict,
    Tuple,
    Dict,
    Mapping,
    Sequence,
)

# ─────────────────────────────────────────────────────────────────────────────
# IDs (single source of truth)
# ─────────────────────────────────────────────────────────────────────────────
NodeId = NewType("NodeId", str)
EdgeId = NewType("EdgeId", str)
LaneId = NewType("LaneId", str)

# ─────────────────────────────────────────────────────────────────────────────
# Ports (canonical names + strong type)
# ─────────────────────────────────────────────────────────────────────────────
PortName = Literal["LEFT_OUT", "RIGHT_IN", "TOP_OUT", "BOTTOM_IN"]

LEFT_OUT: PortName = "LEFT_OUT"
RIGHT_IN: PortName = "RIGHT_IN"
TOP_OUT: PortName = "TOP_OUT"
BOTTOM_IN: PortName = "BOTTOM_IN"


@dataclass(frozen=True)
class PortId:
    """Global identifier for a node's anchor port."""
    node_id: NodeId
    name: PortName

    def key(self) -> str:
        return f"{self.node_id}:{self.name}"


# ─────────────────────────────────────────────────────────────────────────────
# Lanes
# ─────────────────────────────────────────────────────────────────────────────
class Lane(Enum):
    CENTER = 0
    TOP    = 1
    BOTTOM = 2
    LEFT   = 3
    RIGHT  = 4

    def json(self) -> str:  # JSON-friendly
        return self.name.lower()


# Optional: helpers used by layout/adapters
Pos = Tuple[float, float]                       # scene coords (Qt-free)
PlacementMap = Dict[NodeId, Pos]               # layout result


class LaneKey(TypedDict):
    lane: str                                   # 'top'|'bottom'|'left'|'right'|'center'
    index: int


# ─────────────────────────────────────────────────────────────────────────────
# Edge rendering
# ─────────────────────────────────────────────────────────────────────────────
class EdgeMode(Enum):
    STRAIGHT = 1
    ORTHO    = 2
    QUAD     = 3


EdgeRenderMode = Literal["full", "micro", "none"]


# ─────────────────────────────────────────────────────────────────────────────
# Analyzer/adapter shared context (TypedDict for JSON-friendliness)
# ─────────────────────────────────────────────────────────────────────────────
class EdgeReason(TypedDict, total=False):
    """
    Minimal structured reason for an edge.
    Examples:
      kind="import", detail="from pkg.mod import x", weight=1.0
      conditional=True, unresolved=False, ambiguous=False
    """
    kind: str
    detail: str
    weight: float
    conditional: bool
    ambiguous: bool
    unresolved: bool
    symbols: Sequence[str]    # optional, for richer UIs


class EdgeEvidence(TypedDict, total=False):
    """Provenance for why an edge exists."""
    file: str
    line: int
    snippet: str
    confidence: float
    reason: str               # e.g., "ast", "lexical", "fallback"


# ─────────────────────────────────────────────────────────────────────────────
# UI payloads (what Scene/UI emit to panels/docks)
# ─────────────────────────────────────────────────────────────────────────────
class EdgePayload(TypedDict, total=False):
    """
    Normalized edge payload handed to UI consumers.

    Invariants:
      - src and dst are canonical NodeId values (repo-relative POSIX file ids).
      - edge_id is an opaque, stable identifier (if present).
    """
    edge_id: EdgeId
    src: NodeId
    dst: NodeId

    # Optional UI labels
    src_label: str
    dst_label: str

    # Optional module names
    src_module: str
    dst_module: str

    # Optional structured context
    reasons: Sequence[EdgeReason]
    evidence: Sequence[EdgeEvidence]

    # Optional index back into the model list
    edge_index: int
    provenance: str  # "source" | "inferred" | "dynamic" | "fallback" | etc.


class EdgeClickPayload(TypedDict, total=False):
    """
    Minimal payload emitted on EdgeItem click/activation.
    """
    payload: EdgePayload
    evidence: Sequence[EdgeEvidence]


class NodePayload(TypedDict, total=False):
    """
    Normalized node payload handed to UI consumers.

    Invariants (Option 1):
      - node_id is the canonical **file id**, repo-relative POSIX path.
        Example: "scrapy/core/engine.py"
      - file_id MUST equal node_id (redundant but explicit for downstream code).
    """
    node_id: NodeId

    # Explicit alias for clarity in consumers that think in "file ids"
    file_id: str              # must be identical to node_id

    # UI-first identity
    label: str                # preferred display label
    kind: str                 # e.g., "module" | "class" | "function" | "file" | ...

    # Optional structure/metadata (best-effort; analyzer-specific)
    path: str                 # filesystem path (absolute or workspace-relative)
    module: str               # dotted module path (e.g. "scrapy.core.engine")
    package: str
    file: str                 # leaf filename (e.g. "engine.py")
    line: int
    col: int
    size: int                 # e.g. LOC, bytes, or analyzer-defined size metric

    # Optional contents / constituents
    symbols: Sequence[str]    # e.g., exported names, class members, etc.
    extra: Mapping[str, Any]  # analyzer-defined metadata


class NodeClickPayload(TypedDict, total=False):
    """
    Minimal payload emitted on NodeItem click/activation.
    """
    payload: NodePayload
