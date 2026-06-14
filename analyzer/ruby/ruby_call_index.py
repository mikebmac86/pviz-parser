from __future__ import annotations

from typing import Dict, List, NamedTuple, Optional

from analyzer.ruby.parse_ruby.models import RubyAnalysis


class CallEdge(NamedTuple):
    source_file: str         # rel_path of calling file
    source_method: str       # fq_name of calling method
    target: str              # fq_name of the best candidate (method or file)
    target_file: Optional[str]   # rel_path of target, if resolved to a file
    confidence: float
    reason: str


def build_call_edges(
    *,
    analysis: RubyAnalysis,
    method_fq_to_file: Dict[str, str],   # fq_method_name -> rel_path
    min_confidence: float = 0.6,
) -> List[CallEdge]:
    """
    Project the Ruby CallIndex into a list of cross-file CallEdges.

    Only emits edges where:
      - confidence >= min_confidence
      - at least one target_candidate is resolvable to a different file than
        the caller (i.e. genuinely cross-file)

    Dynamic calls are excluded.
    """
    indexes = analysis.indexes or {}
    calls_by_method: Dict[str, List[dict]] = (
        (indexes.get("calls") or {}).get("calls_by_method") or {}
    )

    # Build file -> set of methods lookup for caller-side dedup
    # fq_method -> source_file (from method_index declared entries)
    method_index = indexes.get("methods") or {}
    declared: Dict[str, List[dict]] = method_index.get("declared") or {}

    # fq_name -> first file that declares it (for cross-file check)
    fq_to_file: Dict[str, str] = {}
    for fq, entries in declared.items():
        for entry in (entries or []):
            f = entry.get("file", "")
            if f:
                fq_to_file[str(fq)] = str(f)
                break

    # Merge passed-in override (from folder_index build)
    fq_to_file.update(method_fq_to_file)

    edges: list[CallEdge] = []
    seen: set[tuple] = set()

    for caller_fq, calls in calls_by_method.items():
        # Determine which file the caller lives in
        caller_file = fq_to_file.get(str(caller_fq), "")

        for call in (calls or []):
            if not isinstance(call, dict):
                continue
            if call.get("dynamic"):
                continue

            confidence = float(call.get("confidence") or 0.0)
            if confidence < min_confidence:
                continue

            candidates: List[str] = call.get("target_candidates") or []
            reason: str = call.get("reason") or ""
            source_file: str = str(call.get("file") or caller_file)

            for candidate in candidates:
                target_file = fq_to_file.get(str(candidate))
                # Only emit cross-file edges
                if not target_file or target_file == source_file:
                    continue
                key = (source_file, candidate)
                if key in seen:
                    continue
                seen.add(key)
                edges.append(CallEdge(
                    source_file=source_file,
                    source_method=str(caller_fq),
                    target=str(candidate),
                    target_file=target_file,
                    confidence=confidence,
                    reason=reason,
                ))

    return sorted(edges, key=lambda e: (e.source_file, e.target_file or "", e.target))


def build_method_fq_to_file(analysis: RubyAnalysis) -> Dict[str, str]:
    """
    Build a fq_method_name -> rel_path dict from the parsed files directly
    (not the index) for cases where the index isn't available yet.
    """
    out: Dict[str, str] = {}
    for rel, pf in (analysis.files or {}).items():
        for meth in (pf.methods or []):
            fq = meth.fq_name or meth.name
            if fq:
                out[fq] = str(rel)
    return out