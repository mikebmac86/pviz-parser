# adapters/analyzer_bridge/internals.py
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple, List
import os

from adapters.canonical import (
    to_posix,
    moduleish_for_path,  # POSIX + scan-root relative
)
from diagnostics.logging import log_event  # ← central logger

__all__ = [
    "token_to_module",
    "build_import_edge",
    "edge_dedupe_key",
    "ok",
    "log",
    "attr_or_key",
    "as_list"
]

# ---------------------------------------------------------------------------
# Logging helpers (opt-in via PVIZ_LOG_FILTER)
# ---------------------------------------------------------------------------
_PF = os.getenv("PVIZ_LOG_FILTER")

def ok(*vals) -> bool:
    """
    Backwards-compatible env-based filter:

      • If PVIZ_LOG_FILTER is unset/empty -> always True.
      • Else -> True if any value stringifies to something containing the filter.
    """
    if not _PF:
        return True
    try:
        for v in vals:
            if _PF in str(v):
                return True
    except Exception:
        return True
    return False

def log(*parts: object) -> None:
    """
    Analyzer-bridge logging shim.

    - Respects PVIZ_LOG_FILTER via ok(*parts):
        • If ok(...) is False, nothing is emitted.
    - Routes to the central diagnostics logger as:
        kind = "AN_BRIDGE:log"
        fields = { "msg": "<joined string>" }
    """
    try:
        if not ok(*parts):
            return
        msg = " ".join(str(p) for p in parts)
        if not msg:
            return
        log_event("AN_BRIDGE:log", msg=msg)
    except Exception:
        # Never let diagnostics break analysis
        pass

# helpers (pure)
def as_list(x):
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, (tuple, set)):
        return list(x)
    return [x]

def attr_or_key(obj, name: str):
    try:
        v = getattr(obj, name, None)
    except Exception:
        v = None
    if v is None and isinstance(obj, dict):
        v = obj.get(name)
    return v

# ---------------------------------------------------------------------------
# Module token normalization
# ---------------------------------------------------------------------------

def token_to_module(token: str) -> Optional[str]:
    """Best-effort conversion of dotted or path-like tokens into a dotted module.
    Examples:
      'pkg.sub.mod'      -> 'pkg.sub.mod'
      'pkg/sub/mod.py'   -> 'pkg.sub.mod'
      'pkg/sub/__init__' -> 'pkg.sub'
      'mod'              -> 'mod'
    """
    if not isinstance(token, str):
        return None
    s = token.strip()
    if not s or s == "(none)":
        return None
    try:
        mod = moduleish_for_path(s)
        mod = (mod or "").strip(".")
        return mod or None
    except Exception:
        # Fallback mirrors previous behavior (very defensive)
        s = to_posix(s)
        if s.endswith(".py"):
            s = s[:-3]
        if s.endswith("/__init__"):
            s = s[: -len("/__init__")]
        s = moduleish_for_path(s) or ""
        s = s.strip(".")
        return s or None

# ---------------------------------------------------------------------------
# Edge builders and dedupe keys (provider -> consumer)
# ---------------------------------------------------------------------------

def build_import_edge(
    *,
    imported_id: str,   # provider module/node id
    importer_id: str,   # consumer module/node id
    kind: str = "import",
    src_contact: Optional[Dict[str, Any]] = None,
    dst_contact: Optional[Dict[str, Any]] = None,
    idx: int = 0,
) -> Dict[str, Any]:
    """Canonical import edge dict.
    Direction is always provider (src=imported_id) -> consumer (dst=importer_id).
    Contacts are optional structured dicts; callers may pass strings by wrapping.
    """
    e: Dict[str, Any] = {
        "src": imported_id,
        "dst": importer_id,
        "kind": kind,
        "imported_id": imported_id,
        "importer_id": importer_id,
        "idx": int(idx) if idx is not None else 0,
    }
    if src_contact is not None:
        e["src_contact"] = src_contact
    if dst_contact is not None:
        e["dst_contact"] = dst_contact
    return e


def edge_dedupe_key(e: Dict[str, Any]) -> Tuple[str, str, str, str, str]:
    """Stable tuple key for de-duplicating edges regardless of incidental fields."""
    src = str(e.get("src") or e.get("imported_id") or "")
    dst = str(e.get("dst") or e.get("importer_id") or "")
    kind = str(e.get("kind") or "")
    # Contacts may be strings or dicts; normalize to short strings for the key
    def _c(v: Any) -> str:
        if isinstance(v, dict):
            # prefer common fields if present
            return (
                v.get("symbol")
                or v.get("name")
                or v.get("as")
                or v.get("label")
                or ""
            )
        return str(v) if v is not None else ""

    src_contact = _c(e.get("src_contact"))
    dst_contact = _c(e.get("dst_contact"))
    return (src, dst, kind, src_contact, dst_contact)

# ---- import row -> annotations (from lexical refs) ---------------------------

def build_import_row_annotations(imp_rows: List[str], parsed: Any) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """
    Match string rows to ImportRef entries (when available) to surface tags/lineno/scope.
    Returns (annotations, tag_counts).
    """
    refs: List[Any] = []
    try:
        refs = list(getattr(parsed, "imports_ast", []) or [])
    except Exception:
        refs = []

    if not refs:
        return (
            [{"row": r, "tags": [], "lineno": None, "scope": None,
              "conditional": None, "under_type_checking": None} for r in imp_rows],
            {}
        )

    # Build a map of normalized raw rows -> indices
    ref_specs: List[Dict[str, Any]] = []
    raw_map: Dict[str, List[int]] = {}
    for i, ref in enumerate(refs):
        raw = getattr(ref, "raw", None)
        if not isinstance(raw, str) or not raw.strip():
            continue
        row = raw.strip()
        spec = {
            "row": row,
            "tags": list(getattr(ref, "tags", []) or []),
            "lineno": getattr(ref, "lineno", None),
            "scope": getattr(ref, "scope", None),
            "conditional": getattr(ref, "conditional", None),
            "under_type_checking": getattr(ref, "under_type_checking", None),
        }
        ref_specs.append(spec)
        raw_map.setdefault(_norm_ws(row), []).append(len(ref_specs) - 1)

    used_indices: set[int] = set()
    annotations: List[Dict[str, Any]] = []
    for r in imp_rows:
        key = _norm_ws(r)
        idx: Optional[int] = None
        lst = raw_map.get(key)
        if lst:
            while lst and (lst[0] in used_indices):
                lst.pop(0)
            if lst:
                idx = lst.pop(0)
                used_indices.add(idx)

        if idx is None:
            for j, spec in enumerate(ref_specs):
                if j in used_indices:
                    continue
                if _norm_ws(spec["row"]) == key:
                    idx = j
                    used_indices.add(j)
                    break

        if idx is not None:
            spec = ref_specs[idx]
            annotations.append({
                "row": r,
                "tags": spec["tags"],
                "lineno": spec["lineno"],
                "scope": spec["scope"],
                "conditional": spec["conditional"],
                "under_type_checking": spec["under_type_checking"],
            })
        else:
            annotations.append({
                "row": r,
                "tags": [],
                "lineno": None,
                "scope": None,
                "conditional": None,
                "under_type_checking": None,
            })

    tag_counts: Dict[str, int] = {}
    for spec in ref_specs:
        for t in spec["tags"]:
            tag_counts[t] = tag_counts.get(t, 0) + 1

    return annotations, tag_counts

def _norm_ws(s: str) -> str:
    return " ".join((s or "").split())
