from __future__ import annotations

from typing import Any, Dict, List

JSON = Any
JSONObject = Dict[str, JSON]
JSONArray = List[JSON]

ENUMS: Dict[str, List[str]] = {
    "parse_status": ["ok", "error", "partial"],
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

    # Symbol structure
    "functions": "fn",
    "functions_detailed": "fnd",
    "classes": "cl",
    "classes_detailed": "cld",
    "globals": "g",
    "globals_detailed": "gd",

    # Graph / type data
    "imports": "imp",
    "facts": "fc",
    "public_exports": "pex",
    "declared_types": "dt",
    "declared_types_fq": "dtq",
    "annotations": "ann",

    # Cross-language / Kotlin-enriched NodeFacts fields
    "interfaces": "if",
    "objects": "ob",
    "enums": "en",
    "type_aliases": "ta",
    "imports_all_raw": "iar",
    "exports": "ex",

    # Crosstalk (all languages)
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

    # Metrics (ordered for readability)
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

    # Dependency / structure
    "imports",
    "public_exports",
    "declared_types",
    "declared_types_fq",
    "annotations",

    # Kotlin / JVM declaration fields
    "exports",
    "interfaces",
    "objects",
    "enums",
    "type_aliases",
    "imports_all_raw",

    # Symbol data
    "functions",
    "functions_detailed",
    "classes",
    "classes_detailed",
    "globals",
    "globals_detailed",
    "facts",

    # Crosstalk (multi-language)
    "crosstalk_candidates_py_v1",
    "crosstalk_candidates_ts_v1",
    "crosstalk_candidates_go_v1",
    "crosstalk_candidates_java_v1",
    "crosstalk_candidates_kotlin_v1",
    "crosstalk_candidates_rust_v1",
]