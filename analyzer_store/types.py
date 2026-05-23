from __future__ import annotations

"""
Typed containers for the position-agnostic data layer artifacts.

Schemas
  - FolderIndex:   "folder-index-1.0.0"
  - NodeFacts:     "nodefacts@v1.8"

Notes
  • Use only JSON-friendly field types (str/int/float/bool/None, tuples for immutability).
  • Keep these decoupled from scene/layout and contracts; this is a data-only layer.
  • The saver/loaders live in sibling modules; these dataclasses are shape references.

Version history
  v1.6
    • Baseline structured NodeFacts schema.

  v1.7
    • Schema version updated to "nodefacts@v1.7"
    • Added optional detailed metadata fields:
        - classes_detailed
        - functions_detailed
        - globals_detailed
    • These fields are attached dynamically via setattr() in nodefacts.py
    • Dataclass definition remained unchanged for backward compatibility

  v1.8
    • Schema version updated to "nodefacts@v1.8"
    • Added dual SCC model support:
        - conceptual SCC (full graph)
        - runtime SCC (TYPE_CHECKING excluded)
    • Added structural metric:
        - meta.cycles.tc_inflation
    • Added optional per-node runtime SCC fields:
        - scc_id_runtime
        - scc_size_runtime
    • Dataclass definition remains unchanged; v1.8-only fields may be attached
      dynamically in nodefacts.py for compatibility with older consumers

Compatibility
  • v1.8 is a strict superset of v1.7
  • Loaders must accept:
      - nodefacts@v1.8
      - nodefacts@v1.7
      - nodefacts@v1.6
      - nodefacts:1.5
      - None
  • Serializers must emit:
      - nodefacts@v1.8
"""

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Tuple

# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------
FOLDER_INDEX_SCHEMA = "folder-index-1.1.0"
NODEFACTS_SCHEMA = "nodefacts@v1.9"

# ---------------------------------------------------------------------------
# FolderIndex
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FileEntry:
    id: str
    file: str
    name: str
    parse_status: str
    imports_all: Tuple[str, ...]
    imports_internal: Tuple[str, ...]
    imports_runtime: Tuple[str, ...]
    imports_runtime_internal: Tuple[str, ...]

    loc: Optional[int]
    sloc: Optional[int]
    comment_lines: Optional[int]
    blank_lines: Optional[int]
    comment_pct: Optional[float]

    size_bytes: Optional[int]
    mtime: Optional[str]
    hash: Optional[str]
    import_style_counts: Mapping[str, Optional[int]]

    symbol_internal: Tuple[str, ...] = field(default_factory=tuple)
    error_snippet: Optional[str] = None
    eligible: bool = True

@dataclass(frozen=True)
class FolderIndex:
    schema: str
    meta: Mapping[str, str]
    files: Mapping[str, FileEntry]


# ---------------------------------------------------------------------------
# NodeFacts v1.8
# ---------------------------------------------------------------------------
@dataclass
class NodeFactsNode:
    id: str
    name: str
    imports: Tuple[str, ...]
    exports: Tuple[str, ...]
    classes: Tuple[str, ...]
    functions: Tuple[str, ...]
    globals: Tuple[str, ...]
    gen_seq: int
    importers_count: int
    dependencies_count: int
    scc_id: str
    scc_size: int
    file: str
    hash: Optional[str]

    # Core metrics
    loc: Optional[int]
    sloc: Optional[int] = None
    comment_lines: Optional[int] = None
    blank_lines: Optional[int] = None
    comment_pct: Optional[float] = None

    # Existing fields
    size_bytes: Optional[int] = None
    mtime: Optional[str] = None
    du: Optional[int] = None
    dd: Optional[int] = None
    parse_status: str = "ok"

    crosstalk_candidates_py_v1: Tuple[Mapping[str, Any], ...] = field(default_factory=tuple)

@dataclass(frozen=True)
class NodeFacts:
    schema: str
    meta: Mapping[str, str]
    nodes: Mapping[str, NodeFactsNode]       # keyed by id