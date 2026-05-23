from __future__ import annotations

"""
Validation helpers for FolderIndex and NodeFacts artifacts.

These are pure checks (no side effects) designed for unit tests and debug menus.
They return a list of strings (issues); empty list means pass.
"""

from typing import List

from analyzer_store.types import FolderIndex, NodeFacts


# ---------------------------------------------------------------------------
# FolderIndex validators
# ---------------------------------------------------------------------------

def validate_folder_index(idx: FolderIndex) -> List[str]:
    issues: List[str] = []
    if idx.schema != "folder-index-1.0.0":
        issues.append(f"schema_mismatch:{idx.schema}")
    # imports_internal ⊆ imports_all for every file
    for mid, fe in idx.files.items():
        sa, si = set(fe.imports_all), set(fe.imports_internal)
        if not si.issubset(sa):
            issues.append(f"imports_internal_not_subset:{mid}")
    return issues


# ---------------------------------------------------------------------------
# NodeFacts validators
# ---------------------------------------------------------------------------

def validate_nodefacts(nf: NodeFacts) -> List[str]:
    issues: List[str] = []
    if nf.schema != "nodefacts-1.5.0":
        issues.append(f"schema_mismatch:{nf.schema}")

    nodes = nf.nodes
    # Σin == Σout across nodes
    sum_in = sum(n.importers_count for n in nodes.values())
    sum_out = sum(n.dependencies_count for n in nodes.values())
    if sum_in != sum_out:
        issues.append(f"degree_sum_mismatch:in={sum_in},out={sum_out}")

    # reciprocity: for every A→B in imports, A ∈ exports[B]
    for a, na in nodes.items():
        for b in na.imports:
            nb = nodes.get(b)
            if nb and a not in nb.exports:
                issues.append(f"export_missing:{a}->{b}")

    # SCC coverage: Σ scc_size over unique scc ids == |nodes|
    scc_ids = {n.scc_id for n in nodes.values()}
    sz = 0
    for sid in scc_ids:
        # find one node with this sid to read its scc_size
        for n in nodes.values():
            if n.scc_id == sid:
                sz += n.scc_size
                break
    if sz != len(nodes):
        issues.append(f"scc_size_sum_mismatch:sum={sz},nodes={len(nodes)}")

    # If seed present: exactly one node should have du=0 & dd=0
    seed_du0_dd0 = [n.id for n in nodes.values() if (n.du == 0 and n.dd == 0)]
    meta_seed = (nf.meta or {}).get("seed_id") or ""
    if meta_seed:
        if len(seed_du0_dd0) != 1:
            issues.append(f"seed_distance_zero_count:{len(seed_du0_dd0)}")
    return issues
