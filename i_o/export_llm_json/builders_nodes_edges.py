from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping


def _copy_mapping(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}

    return {
        str(k): v
        for k, v in value.items()
        if isinstance(k, str) and k
    }


def _copy_list(value: Any) -> List[Any]:
    if not isinstance(value, list):
        return []
    return list(value)


def _copy_nonempty_list_field(
    *,
    src: Mapping[str, Any],
    dst: Dict[str, Any],
    key: str,
) -> None:
    val = src.get(key)
    if isinstance(val, list) and val:
        dst[key] = val


def _copy_present_field(
    *,
    src: Mapping[str, Any],
    dst: Dict[str, Any],
    key: str,
) -> None:
    if key in src and src.get(key) is not None:
        dst[key] = src.get(key)


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

        # ------------------------------------------------------------------
        # Core fields
        # ------------------------------------------------------------------
        for k in (
            "lang",
            "language",
            "kind",
            "file_ext",
            "parse_status",
        ):
            _copy_present_field(src=nf, dst=node_entry, key=k)

        # ------------------------------------------------------------------
        # Metrics and graph summary
        #
        # Version notes:
        # - v1.7 adds richer metadata fields, not SCC semantics.
        # - v1.8 adds runtime SCC fields:
        #     scc_id_runtime
        #     scc_size_runtime
        # - v1.9 adds raw/external imports and language_facts.
        # ------------------------------------------------------------------
        for k in (
            "loc",
            "sloc",
            "comment_lines",
            "blank_lines",
            "comment_pct",
            "importers_count",
            "dependencies_count",
            "scc_id",
            "scc_size",
            "scc_id_runtime",
            "scc_size_runtime",
        ):
            if k in nf:
                node_entry[k] = nf.get(k)

        # ------------------------------------------------------------------
        # v1.9 canonical fields
        #
        # These should survive into the final merged bundle. They are not
        # Ruby-specific; Ruby, Rust, Java, Kotlin, Go, Python, and TS/JS can all
        # use them.
        # ------------------------------------------------------------------
        language_facts = _copy_mapping(nf.get("language_facts"))
        if language_facts:
            node_entry["language_facts"] = language_facts

        for key in (
            "imports_all_raw",
            "imports_external",
            "symbol_internal",
        ):
            _copy_nonempty_list_field(src=nf, dst=node_entry, key=key)

        _copy_present_field(src=nf, dst=node_entry, key="eligible")

        # ------------------------------------------------------------------
        # Symbol lists / language-neutral projections
        #
        # Keep these compact top-level fields because the bundle consumers already
        # expect them. Full per-language richness should live in language_facts.
        # ------------------------------------------------------------------
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
            _copy_nonempty_list_field(src=nf, dst=node_entry, key=key)

        # ------------------------------------------------------------------
        # Java / Kotlin / Ruby shared JVM-style or declaration fields
        # ------------------------------------------------------------------
        for key in (
            "package",
            "package_name",
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

        # ------------------------------------------------------------------
        # Kotlin-specific projected fields
        # ------------------------------------------------------------------
        for key in (
            "interfaces",
            "objects",
            "enums",
            "type_aliases",
            "properties",
        ):
            _copy_nonempty_list_field(src=nf, dst=node_entry, key=key)

        # ------------------------------------------------------------------
        # Rust-specific projected fields
        # ------------------------------------------------------------------
        for key in (
            "module_path",
            "crate_name",
        ):
            _copy_present_field(src=nf, dst=node_entry, key=key)

        for key in (
            "structs",
            "traits",
            "impls",
            "mod_declarations",
        ):
            _copy_nonempty_list_field(src=nf, dst=node_entry, key=key)

        # ------------------------------------------------------------------
        # Crosstalk candidates
        # ------------------------------------------------------------------
        for key in (
            "crosstalk_candidates_py_v1",
            "crosstalk_candidates_ts_v1",
        ):
            _copy_nonempty_list_field(src=nf, dst=node_entry, key=key)

        # ------------------------------------------------------------------
        # Additional facts
        #
        # Keep the small generic facts block for compact legacy consumers. Do not
        # duplicate language_facts here.
        # ------------------------------------------------------------------
        facts: Dict[str, Any] = {}

        for k in (
            "gen_seq",
            "du",
            "dd",
            "hash",
            "mtime",
            "size_bytes",
        ):
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

        edge_entry: Dict[str, Any] = {
            "src": src,
            "dst": dst,
            "kind": kind,
        }

        # ------------------------------------------------------------------
        # Canonical/simple edge metadata
        #
        # v1.9+ language builders increasingly emit these directly.
        # ------------------------------------------------------------------
        for key in (
            "subkind",
            "label",
            "spec",
            "reason",
        ):
            val = e.get(key)
            if isinstance(val, str) and val:
                edge_entry[key] = val

        for key in (
            "confidence",
            "weight",
        ):
            if key in e:
                try:
                    edge_entry[key] = float(e.get(key))
                except Exception:
                    pass

        if "synthetic" in e and isinstance(e.get("synthetic"), bool):
            edge_entry["synthetic"] = e.get("synthetic")

        # ------------------------------------------------------------------
        # Rich reason list, used by older Python/TS crosstalk paths.
        # ------------------------------------------------------------------
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

                for k in (
                    "conditional",
                    "ambiguous",
                    "unresolved",
                ):
                    if k in r and isinstance(r.get(k), bool):
                        rr[k] = r.get(k)

                # Preserve optional newer reason fields when present.
                for k in (
                    "reason",
                    "label",
                    "spec",
                    "target",
                    "source",
                ):
                    val = r.get(k)
                    if isinstance(val, str) and val:
                        rr[k] = val

                if "confidence" in r:
                    try:
                        rr["confidence"] = float(r.get("confidence"))
                    except Exception:
                        pass

                if rr:
                    cleaned_reasons.append(rr)

            if cleaned_reasons:
                edge_entry["reasons"] = cleaned_reasons

        # ------------------------------------------------------------------
        # Evidence block
        # ------------------------------------------------------------------
        evidence = e.get("evidence")
        if isinstance(evidence, Mapping):
            ev: Dict[str, Any] = {}

            for k in (
                "kind",
                "token",
                "reason",
                "label",
                "spec",
                "target",
                "source",
            ):
                val = evidence.get(k)
                if isinstance(val, str) and val:
                    ev[k] = val

            if "confidence" in evidence:
                try:
                    ev["confidence"] = float(evidence.get("confidence"))
                except Exception:
                    pass

            if ev:
                edge_entry["evidence"] = ev

        # ------------------------------------------------------------------
        # Crosstalk metadata
        # ------------------------------------------------------------------
        crosstalk = e.get("crosstalk")
        if isinstance(crosstalk, Mapping) and crosstalk:
            edge_entry["crosstalk"] = dict(crosstalk)

        out.append(edge_entry)

    return out