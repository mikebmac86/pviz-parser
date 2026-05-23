# backend/saas_analyzer/analyzer/go/nodefacts_symbols.py
from __future__ import annotations

"""
NodeFacts symbol extraction helper (Go via goextract batch cache).

This file is intentionally tolerant and dependency-light.

Current contract:
  - Python symbol parsing is handled elsewhere (NodeFacts builder still uses its
    existing Python logic). This helper focuses on Go.

Go strategy (Option A):
  - Read symbols from the Goextract batch cache:
      analyzer.go.parse_dispatch.get_go_batch_parser().get(abs_path)

Important:
  - GoBatchParser.load_batch() is "load-once" (no-op after first run).
    Therefore, callers MUST preload the full Go file set up-front.
    If the cache does not contain `path`, this helper returns parse_status="error"
    with empty symbols (best-effort, never raises).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Mapping, Optional, Tuple

from analyzer.go.go_parse_dispatch import get_go_batch_parser


@dataclass(frozen=True)
class NodeFactsSymbols:
    classes: Tuple[str, ...]
    functions: Tuple[str, ...]
    globals: Tuple[str, ...]
    exports: Tuple[str, ...]
    crosstalk_candidates_py_v1: Tuple[Mapping[str, Any], ...]
    loc_code: Optional[int]
    parse_status: str  # "ok" | "warn" | "error"


def _strip_go_method_qualifier(name: str) -> str:
    """
    Normalize Go method names that may be receiver-qualified by goextract.

    Examples:
      - "(*T).M" -> "M"
      - "(T).M"  -> "M"

    Leaves other names unchanged.
    """
    s = (name or "").strip()
    if not s:
        return ""

    # goextract emits qualified method names starting with '(' and containing ")."
    # Keep suffix after last '.'.
    if s.startswith("(") and ")." in s and "." in s:
        return s.rsplit(".", 1)[-1].strip()

    return s


def _stable_unique_strs(vals: Tuple[str, ...] | List[str]) -> Tuple[str, ...]:
    try:
        return tuple(sorted(set(str(v).strip() for v in (vals or ()) if str(v).strip())))
    except Exception:
        out: List[str] = []
        for v in (vals or ()):
            try:
                s = str(v).strip()
                if s:
                    out.append(s)
            except Exception:
                continue
        return tuple(out)


def parse_symbols_for_nodefacts(path: Path, cfg: Any) -> NodeFactsSymbols:
    """
    Parse file symbols for NodeFacts population.

    Go:
      - Uses goextract batch cache (GoBatchParser.get).
      - Returns parse_status:
          "ok"    if cached record exists and says ok
          "error" if cache miss or record says error
      - Never raises.

    Python:
      - Not handled here (returns empty, parse_status="warn").
        This keeps NodeFacts builder behavior "untouched" for Python by ensuring
        this helper does not import/own Python parsing logic.
    """
    p = Path(path)

    # Go path: cache-backed
    if p.suffix.lower() == ".go":
        try:
            bp = get_go_batch_parser()
            gp = bp.get(p)
        except Exception:
            gp = None

        if gp is None:
            # Cache miss: preload didn't include this file (or preload disabled/failed).
            return NodeFactsSymbols(
                classes=(),
                functions=(),
                globals=(),
                exports=(),
                crosstalk_candidates_py_v1=(),
                loc_code=None,
                parse_status="error",
            )

        # Extract + normalize
        try:
            classes = _stable_unique_strs(tuple(getattr(gp, "classes", []) or ()))
        except Exception:
            classes = ()

        try:
            funcs_raw = tuple(getattr(gp, "functions", []) or ())
            funcs_norm = tuple(_strip_go_method_qualifier(str(x)) for x in funcs_raw)
            functions = _stable_unique_strs(list(funcs_norm))
        except Exception:
            functions = ()

        try:
            globals_ = _stable_unique_strs(tuple(getattr(gp, "globals", []) or ()))
        except Exception:
            globals_ = ()

        try:
            exports = _stable_unique_strs(tuple(getattr(gp, "all_exports", []) or ()))
        except Exception:
            exports = ()

        # loc_code
        loc_code: Optional[int]
        try:
            lc = getattr(gp, "loc_code", None)
            loc_code = int(lc) if lc is not None else None
        except Exception:
            loc_code = None

        # parse_status: only ok/error are expected from goextract; treat unknown as warn
        ps = str(getattr(gp, "parse_status", "") or "").strip().lower()
        if ps not in ("ok", "error", "warn"):
            ps = "warn" if ps else "warn"

        return NodeFactsSymbols(
            classes=classes,
            functions=functions,
            globals=globals_,
            exports=exports,
            crosstalk_candidates_py_v1=(),
            loc_code=loc_code,
            parse_status=ps,
        )

    # Non-Go path: do not own Python parsing here (keeps Go module clean).
    return NodeFactsSymbols(
        classes=(),
        functions=(),
        globals=(),
        exports=(),
        crosstalk_candidates_py_v1=(),
        loc_code=None,
        parse_status="warn",
    )
