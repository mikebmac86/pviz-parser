from __future__ import annotations

"""
Typed containers for the position-agnostic data layer artifacts.

Schemas
  - FolderIndex:   "folder-index-1.2.0"
  - NodeFacts:     "nodefacts@v1.9"

Notes
  • Use only JSON-friendly field types (str/int/float/bool/None, tuples for
    immutability).
  • Keep these decoupled from scene/layout and contracts; this is a data-only
    layer.
  • The saver/loaders live in sibling modules; these dataclasses are shape
    references.
  • Keep the core schema language-neutral. Language-specific facts should live
    under language_facts, keyed by language name.

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
    • Dataclass definition remained unchanged for backward compatibility.

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
    • Dataclass definition remained mostly unchanged; v1.8-only fields may be
      attached dynamically in nodefacts.py for compatibility with older consumers.

  v1.9
    • Schema version updated to "nodefacts@v1.9"
    • Added language-neutral extension support:
        - FileEntry.imports_external
        - FileEntry.language_facts
        - NodeFactsNode.imports_all_raw
        - NodeFactsNode.imports_external
        - NodeFactsNode.language_facts
    • Purpose:
        - imports_all / imports_all_raw preserve raw import/require/use specs.
        - imports_internal / imports represent resolved internal file targets.
        - imports_external preserves unresolved external specs.
        - language_facts stores language-specific parser facts without polluting
          the core schema.
    • Resolved dependency relationships remain edge records. Import edges should
      carry label/spec/reason/confidence metadata where available.

Compatibility
  • v1.9 is intended to be a strict serialized-schema superset of v1.8.
  • New dataclass fields should be appended near the end of existing classes
    where practical, because some analyzer paths may still construct these
    objects positionally.
  • Loaders must accept:
      - nodefacts@v1.9
      - nodefacts@v1.8
      - nodefacts@v1.7
      - nodefacts@v1.6
      - nodefacts:1.5
      - None
  • Serializers should emit:
      - nodefacts@v1.9
"""

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Tuple

# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------

FOLDER_INDEX_SCHEMA = "folder-index-1.2.0"
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

    # Raw import/require/use surface as seen by the language analyzer.
    imports_all: Tuple[str, ...]

    # Resolved internal file targets. These are graph-facing dependencies.
    imports_internal: Tuple[str, ...]

    # Runtime-only import surface and resolved runtime-only internal targets.
    # For languages without a conceptual/runtime distinction, these may mirror
    # imports_all/imports_internal.
    imports_runtime: Tuple[str, ...]
    imports_runtime_internal: Tuple[str, ...]

    # Core metrics.
    loc: Optional[int] = None
    sloc: Optional[int] = None
    comment_lines: Optional[int] = None
    blank_lines: Optional[int] = None
    comment_pct: Optional[float] = None

    size_bytes: Optional[int] = None
    mtime: Optional[str] = None
    hash: Optional[str] = None
    import_style_counts: Mapping[str, Optional[int]] = field(default_factory=dict)

    # Language-neutral symbol surface.
    symbol_internal: Tuple[str, ...] = field(default_factory=tuple)

    # v1.9: External/unresolved import specs after internal resolution.
    # This is language-neutral and should contain raw specs, not file paths.
    imports_external: Tuple[str, ...] = field(default_factory=tuple)

    # v1.9: Language-specific facts, keyed by language.
    #
    #   language_facts["ruby"] = {
    #       "requires": [...],
    #       "methods": [...],
    #       "declarations": [...],
    #       "references": [...],
    #       "dynamic_requires": [...],
    #       "rails": {...},
    #   }
    #
    #   language_facts["rust"] = {
    #       "use_statements": [...],
    #       "traits": [...],
    #       "impls": [...],
    #       "derives": [...],
    #       "attributes": [...],
    #   }
    #
    # Keep values JSON-friendly.
    language_facts: Mapping[str, Any] = field(default_factory=dict)

    error_snippet: Optional[str] = None
    eligible: bool = True


@dataclass(frozen=True)
class FolderIndex:
    schema: str
    meta: Mapping[str, Any]
    files: Mapping[str, FileEntry]


# ---------------------------------------------------------------------------
# NodeFacts v1.9
# ---------------------------------------------------------------------------

@dataclass
class NodeFactsNode:
    id: str
    name: str

    # Graph-facing internal dependencies.
    imports: Tuple[str, ...]

    exports: Tuple[str, ...] = field(default_factory=tuple)
    classes: Tuple[str, ...] = field(default_factory=tuple)
    functions: Tuple[str, ...] = field(default_factory=tuple)
    globals: Tuple[str, ...] = field(default_factory=tuple)

    gen_seq: int = 0
    importers_count: int = 0
    dependencies_count: int = 0

    # Conceptual SCC fields.
    scc_id: str = ""
    scc_size: int = 1

    # Runtime SCC fields. These may match conceptual SCCs for languages without
    # a runtime/type-only distinction.
    scc_id_runtime: Optional[str] = None
    scc_size_runtime: Optional[int] = None

    file: str = ""
    hash: Optional[str] = None

    # Core metrics.
    loc: Optional[int] = None
    sloc: Optional[int] = None
    comment_lines: Optional[int] = None
    blank_lines: Optional[int] = None
    comment_pct: Optional[float] = None

    size_bytes: Optional[int] = None
    mtime: Optional[str] = None
    du: Optional[int] = None
    dd: Optional[int] = None
    parse_status: str = "ok"

    # v1.9: Raw import/require/use specs preserved from the analyzer.
    imports_all_raw: Tuple[str, ...] = field(default_factory=tuple)

    # v1.9: External/unresolved specs after internal resolution.
    imports_external: Tuple[str, ...] = field(default_factory=tuple)

    # v1.9: Language-specific facts, keyed by language name. This is the
    # preferred place for Ruby methods/references/requires, Rust traits/impls/
    # derives, Java annotations/packages, etc.
    language_facts: Mapping[str, Any] = field(default_factory=dict)

    crosstalk_candidates_py_v1: Tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    crosstalk_candidates_ts_v1: Tuple[Mapping[str, Any], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class NodeFacts:
    schema: str
    meta: Mapping[str, Any]
    nodes: Mapping[str, NodeFactsNode]  # keyed by id