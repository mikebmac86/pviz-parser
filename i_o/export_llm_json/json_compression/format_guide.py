# i_o/json_compression/format_guide.py
"""
Static format guide for self-documenting compressed JSON.

Included in compressed output so an LLM can decode and interpret the bundle
without external documentation. Keep this concise: it is prompt context, not
full developer documentation.
"""

from __future__ import annotations

from typing import Any, Dict


FORMAT_GUIDE: Dict[str, Any] = {
    "format_version": "pviz-llm-bundle@v1.2",
    "compression": "schema rows + legends + path legend",
    "lossless": True,

    "how_to_decode": {
        "schema_rows": (
            "For nodes or edges, each row is an array. Decode field names with "
            "field_name = legend[schema[position]]."
        ),
        "path_legend": (
            "Internal repo paths are stored in path_legend. For a path id, "
            "full_path = prefixes[paths[id][0]] + paths[id][1]."
        ),
        "negative_path_refs": (
            "Negative integers may be path refs outside enum/type contexts. "
            "Decode with path_id = (-value) - 1."
        ),
        "direct_path_ids": (
            "Non-negative integers in direct path fields such as file, node_id, "
            "imports, imports_internal, and symbol_internal are direct path ids."
        ),
        "enums": (
            "parse_status and param_kind use the local enums table. Edge kind uses "
            "edges.kinds."
        ),
        "types": (
            "type_hint and return_type may be integer indexes into type_strings."
        ),
        "nested": (
            "Some fields, such as functions_detailed, classes_detailed, "
            "globals_detailed, and crosstalk_candidates_*_v1, may contain their own "
            "schema/legend/rows structures."
        ),
    },

    "important_node_fields": {
        "imports": "Resolved internal dependency targets.",
        "imports_all_raw": "Raw import/use/require specs as seen by the analyzer.",
        "imports_external": "External or unresolved import specs after internal resolution.",
        "symbol_internal": "Internal symbol/crosstalk targets separate from explicit imports.",
        "language_facts": (
            "Rich per-language analyzer facts keyed by language name. These preserve "
            "language-specific detail beyond the compact top-level projection."
        ),
        "facts": "Compact generic facts preserved for legacy or lightweight consumers.",
    },

    "edge_fields": {
        "src": "Source node id.",
        "dst": "Destination node id.",
        "kind": "Edge kind, e.g. import, call, reference, inherit, uses, exports.",
        "label": "Human-readable edge label, often the raw import/use/require spec.",
        "spec": "Raw language-level import/use/require spec that caused or describes the edge.",
        "reason": "Machine-readable reason for edge creation or resolution.",
        "confidence": "Analyzer confidence score when available.",
        "weight": "Graph weight when available.",
        "subkind": "Optional edge subtype.",
        "synthetic": "True when the edge was synthesized rather than directly observed.",
        "evidence": "Optional structured evidence object.",
        "reasons": "Optional list of richer reason/evidence objects.",
        "crosstalk": "Optional cross-language relationship metadata.",
    },

    "path_vs_semantic_strings": {
        "rule": (
            "Do not assume every string that resembles an import is a file path. "
            "Raw specs, labels, names, fq_names, owners, and reasons are semantic "
            "strings unless encoded as path ids/path refs."
        ),
        "path_like_fields": [
            "file",
            "node_id",
            "src",
            "dst",
            "imports",
            "imports_internal",
            "symbol_internal",
            "target",
            "source",
            "owner_file",
            "provider_file",
            "provider_files",
            "resolved_target",
            "resolved_file",
        ],
        "semantic_fields": [
            "imports_all_raw",
            "imports_external",
            "spec",
            "label",
            "reason",
            "fq_name",
            "name",
            "owner",
            "kind",
            "visibility",
            "raw",
            "call_form",
            "receiver",
        ],
        "important_import_distinction": (
            "imports are resolved internal graph targets. imports_all_raw are raw "
            "source-level specs. imports_external are unresolved/external specs. "
            "Do not count imports_all_raw as external dependencies."
        ),
    },

    "cycle_fields": {
        "scc_id": (
            "Identifier for the strongly connected component in the conceptual graph."
        ),
        "scc_size": (
            "Number of nodes in the conceptual SCC. Values greater than 1 indicate "
            "participation in a cycle."
        ),
        "scc_id_runtime": (
            "Identifier for the runtime-only SCC when emitted. This excludes "
            "type-only or otherwise non-runtime dependency edges where supported."
        ),
        "scc_size_runtime": (
            "Runtime SCC size when emitted. Compare with scc_size to distinguish "
            "runtime cycle risk from broader conceptual coupling."
        ),
        "interpretation": (
            "Use scc_size for general architectural coupling. Use scc_size_runtime "
            "for runtime import/load-cycle risk when runtime SCC fields are present."
        ),
    },

    "summary_cycles": {
        "cycle_nodes": "Total nodes participating in non-trivial SCCs.",
        "largest_scc_size": "Size of the largest SCC.",
        "scc_conceptual": "Cycle metrics for the full conceptual dependency graph when present.",
        "scc_runtime": "Cycle metrics for runtime-only dependency graph when present.",
        "tc_inflation": (
            "Difference between conceptual and runtime largest SCC sizes where present. "
            "A positive value means type-only/non-runtime edges inflate apparent cycles."
        ),
    },

    "minimal_examples": {
        "node_row": {
            "schema": ["f", "id", "n", "ps", "imp"],
            "legend": {
                "f": "file",
                "id": "node_id",
                "n": "name",
                "ps": "parse_status",
                "imp": "imports",
            },
            "row_meaning": (
                "file/node_id/imports are path ids; parse_status is an enum index."
            ),
        },
        "edge_row": {
            "schema": ["s", "d", "k", "c", "w", "l", "p", "r"],
            "legend": {
                "s": "src",
                "d": "dst",
                "k": "kind",
                "c": "confidence",
                "w": "weight",
                "l": "label",
                "p": "spec",
                "r": "reason",
            },
            "row_meaning": (
                "src/dst are path ids or path refs; kind is edges.kinds[index]; "
                "label/spec/reason are semantic strings."
            ),
        },
    },

    "quick_reference": {
        "field_name": "legend[schema[i]]",
        "negative_path_ref": "path_id = (-value) - 1",
        "direct_path_id": "path_legend.paths[value]",
        "enum": "enums[field][value]",
        "edge_kind": "edges.kinds[value]",
        "type_string": "type_strings[value]",
    },
}


def get_format_guide() -> Dict[str, Any]:
    return FORMAT_GUIDE