from __future__ import annotations

from typing import Any, Dict, List

JSON = Any
JSONObject = Dict[str, JSON]
JSONArray = List[JSON]

ENUMS: Dict[str, List[str]] = {
    "parse_status": ["ok", "error", "partial", "warn"],
    "param_kind": [
        "positional_or_keyword",
        "positional_only",
        "keyword_only",
        "var_positional",
        "var_keyword",
    ],
}

KNOWN_FIELD_ABBRS: Dict[str, str] = {
    "file": "f",
    "node_id": "id",
    "name": "n",
    "module_guess": "m",
    "language": "lang",
    "lang": "lg",
    "file_ext": "ext",
    "package": "pkg",
    "package_name": "pkgn",

    # Metrics
    "loc": "loc",
    "sloc": "sloc",
    "comment_lines": "com",
    "blank_lines": "blk",
    "comment_pct": "cp",

    "parse_status": "ps",
    "importers_count": "ic",
    "dependencies_count": "dc",
    "scc_id": "sid",
    "scc_size": "ssz",
    "scc_id_runtime": "rsid",
    "scc_size_runtime": "rssz",

    # Symbol structure
    "functions": "fn",
    "functions_detailed": "fnd",
    "classes": "cl",
    "classes_detailed": "cld",
    "globals": "g",
    "globals_detailed": "gd",

    # Graph / type data
    "imports": "imp",
    "imports_all_raw": "iar",
    "imports_external": "iex",
    "symbol_internal": "symi",
    "facts": "fc",
    "language_facts": "lf",
    "eligible": "el",

    "public_exports": "pex",
    "declared_types": "dt",
    "declared_types_fq": "dtq",
    "annotations": "ann",
    "exports": "ex",

    # Cross-language / language-enriched NodeFacts fields
    "interfaces": "if",
    "objects": "ob",
    "enums": "en",
    "type_aliases": "ta",
    "properties": "pr",

    # Rust / Go / module-style fields
    "module_path": "mp",
    "crate_name": "cr",
    "structs": "st",
    "traits": "tr",
    "impls": "im",
    "mod_declarations": "mods",

    # Crosstalk
    "crosstalk_candidates_py_v1": "ctpy",
    "crosstalk_candidates_ts_v1": "ctts",
    "crosstalk_candidates_go_v1": "ctgo",
    "crosstalk_candidates_java_v1": "ctja",
    "crosstalk_candidates_kotlin_v1": "ctkt",
    "crosstalk_candidates_rust_v1": "ctrs",
}

PREFERRED_NODE_FIELDS: List[str] = [
    "file",
    "node_id",
    "name",
    "language",
    "lang",
    "file_ext",
    "module_guess",
    "package",
    "package_name",

    # Metrics
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

    # Dependency / graph structure
    "imports",
    "imports_all_raw",
    "imports_external",
    "symbol_internal",

    # Declaration / export structure
    "public_exports",
    "declared_types",
    "declared_types_fq",
    "annotations",
    "exports",

    # Language-specific projected declaration fields
    "interfaces",
    "objects",
    "enums",
    "type_aliases",
    "properties",

    # Rust / module-style projected fields
    "module_path",
    "crate_name",
    "structs",
    "traits",
    "impls",
    "mod_declarations",

    # Symbol data
    "functions",
    "functions_detailed",
    "classes",
    "classes_detailed",
    "globals",
    "globals_detailed",

    # Rich language facts and compact generic facts
    "language_facts",
    "facts",
    "eligible",

    # Crosstalk
    "crosstalk_candidates_py_v1",
    "crosstalk_candidates_ts_v1",
    "crosstalk_candidates_go_v1",
    "crosstalk_candidates_java_v1",
    "crosstalk_candidates_kotlin_v1",
    "crosstalk_candidates_rust_v1",
]