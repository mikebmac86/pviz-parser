from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping


def build_nodes(nodefacts: Mapping[str, Any]) -> Dict[str, Any]:
    raw_nodes = nodefacts.get("nodes", {}) or {}
    out: Dict[str, Any] = {}

    if not isinstance(raw_nodes, Mapping):
        return out

    for node_id, nf in raw_nodes.items():
        if not isinstance(node_id, str) or not node_id:
            continue
        if not isinstance(nf, Mapping):
            continue

        file_path = nf.get("file", node_id)
        name = nf.get("name") or str(node_id).rsplit("/", 1)[-1]

        node_entry: Dict[str, Any] = {
            "node_id": node_id,
            "file": file_path,
            "name": name,
        }

        # Best-effort module guess (non-authoritative)
        try:
            p = Path(str(file_path).replace("\\", "/"))
            if p.suffix.lower() == ".py":
                p = p.with_suffix("")
            node_entry["module_guess"] = str(p).replace("/", ".")
        except Exception:
            pass

        # Core fields (all languages)
        for k in ("lang", "language", "kind", "file_ext"):
            if k in nf and nf.get(k) is not None:
                node_entry[k] = nf.get(k)

        # Metrics (all languages)
        # Version notes:
        # - v1.7 adds richer metadata fields, not SCC semantics
        # - v1.8 adds runtime SCC fields:
        #     scc_id_runtime
        #     scc_size_runtime
        for k in (
            "loc",
            "sloc",
            "comment_lines",
            "blank_lines",
            "comment_pct",
            "parse_status",
            "importers_count",
            "dependencies_count",
            "scc_id",
            "scc_size",
            "scc_id_runtime",
            "scc_size_runtime",
        ):
            if k in nf:
                node_entry[k] = nf.get(k)

        # Symbol lists (all languages)
        # v1.7 detailed metadata fields:
        #   functions_detailed, classes_detailed, globals_detailed
        # v1.8 does not add symbol metadata fields; it adds runtime SCC fields above.
        for key in (
            "functions",
            "classes",
            "globals",
            "exports",
            "imports",
            "functions_detailed",
            "classes_detailed",
            "globals_detailed",
        ):
            val = nf.get(key)
            if isinstance(val, list) and val:
                node_entry[key] = val

        # ========================================================================
        # JAVA-SPECIFIC FIELDS
        # ========================================================================
        for key in ("package", "declared_types", "declared_types_fq", "public_exports", "annotations"):
            val = nf.get(key)
            if val is not None:
                if isinstance(val, list):
                    if val:
                        node_entry[key] = val
                else:
                    node_entry[key] = val
        # ========================================================================

# ========================================================================
        # KOTLIN-SPECIFIC FIELDS
        # ========================================================================
        # List fields: only include if non-empty
        for key in (
            "interfaces",
            "objects",
            "enums",
            "type_aliases",
            "properties",
            "annotations",
            "imports_all_raw",
            "imports_external",
        ):
            val = nf.get(key)
            if isinstance(val, list) and val:
                node_entry[key] = val

        # Scalar fields: include if present and non-None
        for key in ("package_name",):
            val = nf.get(key)
            if val is not None:
                node_entry[key] = val
        # ========================================================================

        # ========================================================================
        # RUST-SPECIFIC FIELDS
        # ========================================================================
        for key in (
            "module_path",
            "crate_name",
            "declared_types",
            "declared_types_fq",
            "public_exports",
            "annotations",
        ):
            val = nf.get(key)
            if val is not None:
                if isinstance(val, list):
                    if val:
                        node_entry[key] = val
                else:
                    node_entry[key] = val

        for key in ("structs", "enums", "traits", "impls", "type_aliases", "mod_declarations"):
            val = nf.get(key)
            if isinstance(val, list) and val:
                node_entry[key] = val
        # ========================================================================

        # ========================================================================
        # CROSSTALK CANDIDATES (Python ↔ TypeScript)
        # ========================================================================
        # Preserve crosstalk candidates from both Python and TypeScript analyzers.
        for key in ("crosstalk_candidates_py_v1", "crosstalk_candidates_ts_v1"):
            val = nf.get(key)
            if isinstance(val, list) and val:
                node_entry[key] = val
        # ========================================================================

        # Additional facts
        facts: Dict[str, Any] = {}
        for k in ("gen_seq", "du", "dd", "hash", "mtime", "size_bytes"):
            if k in nf and nf.get(k) is not None:
                facts[k] = nf.get(k)

        imp = nf.get("imports")
        exp = nf.get("exports")
        if isinstance(imp, list):
            facts["imports"] = imp
        elif imp is not None and "imports" not in facts:
            facts["imports_raw"] = imp

        if isinstance(exp, list):
            facts["exports"] = exp
        elif exp is not None and "exports" not in facts:
            facts["exports_raw"] = exp

        if facts:
            node_entry["facts"] = facts

        out[node_id] = node_entry

    return out


def build_edges(edges_data: Mapping[str, Any]) -> List[Dict[str, Any]]:
    edges_raw = edges_data.get("edges", []) or []
    if not isinstance(edges_raw, list):
        return []

    out: List[Dict[str, Any]] = []

    for e in edges_raw:
        if not isinstance(e, Mapping):
            continue

        src = e.get("src")
        dst = e.get("dst")
        if not isinstance(src, str) or not src:
            continue
        if not isinstance(dst, str) or not dst:
            continue

        kind = e.get("kind", "import")
        edge_entry: Dict[str, Any] = {"src": src, "dst": dst, "kind": kind}

        if "subkind" in e and isinstance(e.get("subkind"), str):
            edge_entry["subkind"] = e.get("subkind")
        if "synthetic" in e and isinstance(e.get("synthetic"), bool):
            edge_entry["synthetic"] = e.get("synthetic")

        reasons = e.get("reasons")
        if isinstance(reasons, list) and reasons:
            cleaned_reasons: List[Dict[str, Any]] = []
            for r in reasons:
                if not isinstance(r, Mapping):
                    continue
                rr: Dict[str, Any] = {}
                syms = r.get("symbols")
                if isinstance(syms, list):
                    rr["symbols"] = syms
                for k in ("conditional", "ambiguous", "unresolved"):
                    if k in r and isinstance(r.get(k), bool):
                        rr[k] = r.get(k)
                if rr:
                    cleaned_reasons.append(rr)
            if cleaned_reasons:
                edge_entry["reasons"] = cleaned_reasons

        evidence = e.get("evidence")
        if isinstance(evidence, Mapping):
            ev: Dict[str, Any] = {}
            if isinstance(evidence.get("kind"), str):
                ev["kind"] = evidence.get("kind")
            if isinstance(evidence.get("token"), str):
                ev["token"] = evidence.get("token")
            if isinstance(evidence.get("reason"), str):
                ev["reason"] = evidence.get("reason")
            if "confidence" in evidence:
                c = evidence.get("confidence")
                try:
                    ev["confidence"] = float(c)
                except Exception:
                    pass
            if ev:
                edge_entry["evidence"] = ev

        crosstalk = e.get("crosstalk")
        if isinstance(crosstalk, Mapping) and crosstalk:
            edge_entry["crosstalk"] = dict(crosstalk)

        out.append(edge_entry)

    return out