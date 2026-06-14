from __future__ import annotations

from typing import Dict, List, NamedTuple, Optional

from analyzer.ruby.ruby_canonical import ResolveResult, resolve_ruby_require


class RequireEdge(NamedTuple):
    source: str          # rel_path of requiring file
    target: str          # rel_path of required file
    spec: str            # normalized require spec
    kind: str            # "require" | "require_relative" | "load"
    reason: str          # from ResolveResult


def build_require_edges(
    *,
    parsed_files: Dict[str, any],   # rel_path -> RubyParsedFile
    require_spec_to_files: Dict[str, List[str]],
    fq_decl_to_file: Dict[str, str],
) -> List[RequireEdge]:
    """
    Walk every parsed file's requires list and resolve each spec to internal
    file targets.  Returns a deduplicated list of RequireEdges.

    Only emits internal edges (kind="internal" ResolveResults).
    Dynamic requires are skipped.
    """
    edges: list[RequireEdge] = []
    seen: set[tuple] = set()

    for rel, pf in parsed_files.items():
        requires = getattr(pf, "requires", None) or []
        for req in requires:
            if req.dynamic or not req.spec:
                continue

            results: List[ResolveResult] = resolve_ruby_require(
                spec=req.spec,
                kind=req.kind,
                require_spec_to_files=require_spec_to_files,
                fq_decl_to_file=fq_decl_to_file,
            )

            for r in results:
                if r.kind != "internal" or not r.resolved:
                    continue
                key = (rel, r.resolved, req.kind)
                if key in seen:
                    continue
                seen.add(key)
                edges.append(RequireEdge(
                    source=rel,
                    target=r.resolved,
                    spec=r.spec,
                    kind=req.kind,
                    reason=r.reason,
                ))

    return edges


def build_require_spec_to_files(require_index):
    out = {}
    by_provider = require_index.get("by_provider") or {}
    for spec, files in (by_provider.items() if isinstance(by_provider, dict) else []):
        if spec and isinstance(files, list):
            out[str(spec)] = sorted(str(f) for f in files if f)
    return out