#analyzer/ts/model.py
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass(frozen=True)
class RawImport:
    spec: str
    kind: str  # "import" | "reexport" | "require" | "dynamic_import"
    symbols: List[str]
    loc: Optional[Tuple[int, int]] = None  # (line, col) 0-based if available


@dataclass(frozen=True)
class ResolvedImport:
    src_node_id: str
    spec: str
    kind: str
    symbols: List[str]
    dst_node_id: Optional[str]  # resolved internal node id, else None
    unresolved: bool
    loc: Optional[Tuple[int, int]] = None