# analyzer/types.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
import ast

# ----------------------------
# Type aliases (annotations only; no runtime impact)
# ----------------------------
FileId = str      # repo-relative POSIX path (e.g., "pkg/mod.py")
ModuleId = str    # dotted module name (e.g., "pkg.mod")

# ----------------------------
# Symbols shown inside a node
# ----------------------------

@dataclass
class SymbolSummary:
    name: str
    kind: str  # "class" | "function" | "global"
    extra: Optional[dict] = None

# ----------------------------
# Per-file metadata
# ----------------------------

@dataclass
class ParsedModule:
    """
    Lightweight per-file parse product used by downstream steps.
    Compatible with both the edge builder and index consumers.
    """
    # Definitions/exports
    classes: List[SymbolSummary] = field(default_factory=list)
    functions: List[SymbolSummary] = field(default_factory=list)
    globals: List[SymbolSummary] = field(default_factory=list)

    # Lexical import refs (from imports_lex.extract_lexical_imports)
    imports_ast: List[ImportRef] = field(default_factory=list)

    loc: Dict[str, int] = field(default_factory=dict)
    loc_code: Optional[int] = None

    # __all__ and parser warnings
    all_exports: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    # Crosstalk candidates (Python -> TS/JS), Level 1 facts only
    # Populated by analyzer/python/extract_crosstalk.py
    crosstalk_candidates_py_v1: List[Dict[str, Any]] = field(default_factory=list)

    # >>> Fields required by the edge builder <<<
    # Top-level ast.Import / ast.ImportFrom statements
    imports: List[ast.stmt] = field(default_factory=list)
    # Lists of import statements that were under `if TYPE_CHECKING:`
    type_checking_blocks: List[List[ast.stmt]] = field(default_factory=list)

@dataclass
class ImportRowMeta:
    """
    Metadata for a single textual import row (node.imports_rows[i]).
    - parsed: True when the row could be parsed into module/name tuples
    - external_only: True when all candidate modules are outside the workspace
    - candidates: absolute candidate modules derived from the row (for debug/hover)
    """
    parsed: bool = False
    external_only: bool = False
    candidates: List[str] = field(default_factory=list)

@dataclass
class FileNode:
    # Identity
    id: FileId         # repo-relative POSIX path (e.g., "pkg/mod.py")
    module: ModuleId   # dotted module name (e.g., "pkg.mod")
    path: str          # absolute path on disk (OS-native)

    # Parsed summaries
    classes: List[SymbolSummary] = field(default_factory=list)
    functions: List[SymbolSummary] = field(default_factory=list)
    globals: List[SymbolSummary] = field(default_factory=list)

    # Text rows for left/right columns in the card
    imports_rows: List[str] = field(default_factory=list)  # e.g., ["from x import y", "import z"]
    imports_meta: List[ImportRowMeta] = field(default_factory=list)
    defs_rows: List[str] = field(default_factory=list)     # e.g., ["class Foo", "def bar", "BAZ = 1"]

    # Diagnostics / badges
    warnings: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    # Crosstalk candidates (Python -> TS/JS), Level 1 facts only
    crosstalk_candidates_py_v1: List[Dict[str, Any]] = field(default_factory=list)

    # Optional: stash parser product for downstream edge-building
    parsed: Any = None

# ----------------------------------------------------
# Dual-index additions (AST + Lexical import modeling)
# ----------------------------------------------------

@dataclass
class ImportRef:
    """
    A normalized import record captured from AST with lexical fidelity.
    """
    raw: str
    module_token: Optional[str]                 # e.g. "pkg.sub" or None for 'from . import X'
    names: List[Tuple[str, Optional[str]]]      # [(imported_name, alias)]
    is_from: bool
    level: int                                  # 0 absolute, >0 relative dots
    under_type_checking: bool
    lineno: int
    end_lineno: int
    conditional: bool = False
    scope: str = "module"
    tags: List[str] = field(default_factory=list)

@dataclass
class SemanticEntry:
    """
    Per-file semantic index entry used by the resolver pipeline.
    """
    file_id: FileId
    path: str                    # absolute path (string to align with FileNode.path)
    module_physical: ModuleId    # retains '__init__' for package files (e.g., 'pkg.sub.__init__')
    module_logical: ModuleId     # collapsed package name (e.g., 'pkg.sub')
    defs: Dict[str, List[str]] | Any = field(default_factory=dict)
    imports_ast: List[ImportRef] = field(default_factory=list)

@dataclass
class NameMap:
    """
    Alias dictionaries for diagnostics and tooling.
      - per_file_aliases: file_id -> {local_alias -> fully_qualified_target}
      - alias_to_modules: alias -> list of fully qualified targets (global view)
      - module_to_aliases: fully qualified target -> list of aliases (global view)

    Lists (not sets) are used for stable ordering and JSON-compatibility.
    """
    per_file_aliases: Dict[FileId, Dict[str, ModuleId]] = field(default_factory=dict)
    alias_to_modules: Dict[str, List[ModuleId]] = field(default_factory=dict)
    module_to_aliases: Dict[ModuleId, List[str]] = field(default_factory=dict)

@dataclass
class EdgeEvidence:
    """
    Optional provenance attached to an edge for explainability.
      - kind: "lexical" | "ast"
      - token: the raw import text line(s)
      - confidence: 1.0 for exact logical match; <1.0 for prefix/fallback matches
      - reason: "exact-logical" | "longest-prefix" | "fallback" (expand as needed)
    """
    kind: str
    token: str
    confidence: float
    reason: str

# ----------------------------
# Edge model (file -> file)
# ----------------------------

@dataclass
class EdgeReason:
    """
    Captures WHY an edge exists. For import-only mode:
      - symbols: imported names (or [module] if unknown)
      - conditional: True if came from a typing/conditional context
      - ambiguous: True for star imports or unresolved details
      - unresolved: reserved for future richer analysis (symbol->def not found)
    """
    symbols: List[str] = field(default_factory=list)
    conditional: bool = False
    ambiguous: bool = False
    unresolved: bool = False

@dataclass
class Edge:
    """
    Directed relationship between two FileNodes.
    Convention: src -> dst means "src provides, dst consumes".
    For import edges: src = defining module (producer), dst = importer (consumer).
    """
    src: FileId
    dst: FileId
    reasons: List[EdgeReason] = field(default_factory=list)
    evidence: Optional[EdgeEvidence] = None

# ----------------------------
# Layout + Graph
# ----------------------------

@dataclass
class Layout:
    positions: Dict[FileId, Tuple[float, float]] = field(default_factory=dict)
    zoom: float = 1.0

@dataclass
class Graph:
    nodes: Dict[FileId, FileNode] = field(default_factory=dict)
    edges: List[Edge] = field(default_factory=list)
    layout: Layout = field(default_factory=Layout)
    root: Optional[str] = None
    unresolved_facade_exports: set[FileId] = field(default_factory=set)
    analyzer_cfg: Any = None  # Optional: snapshot of AnalyzerCfg or derived settings

@dataclass(frozen=True)
class AnalysisResult:
    """
    Stable product of the analyzer layer for downstream consumers.
    """
    root: str                                   # absolute scan root (stringified Path)
    files: List[FileId]                         # consistent with FileNode.id
    sem_idx: Dict[FileId, SemanticEntry]        # file_id -> SemanticEntry
    lex_idx: Dict[FileId, List[ImportRef]]      # file_id -> ImportRef[]
    name_map: NameMap                           # alias maps (global + per-file)
    diagnostics: Dict[FileId, List[str]] = field(default_factory=dict)  # file_id -> messages
