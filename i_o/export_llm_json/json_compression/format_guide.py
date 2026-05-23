# i_o/json_compression/format_guide.py
"""
Static format guide for self-documenting compressed JSON.

This guide is included in compressed output to help LLMs decode the format
without external documentation. The overhead is ~1KB (negligible for typical
500KB-10MB files).
"""

FORMAT_GUIDE = {
    "format_version": "pviz-llm-bundle@v1.1",
    "compression": "schema-based with inline decoding information",
    "lossless": True,
    
    "decoding_layers": {
        "1_schema_legend": {
            "what": "Field names abbreviated to 1-3 characters",
            "how": "Use 'schema' array for field order, 'legend' dict to decode abbreviations",
            "example": "schema=['f','n'] legend={'f':'file','n':'name'} row=[0,'main'] -> {file:0, name:'main'}"
        },
        
        "2_path_compression": {
            "what": "File paths compressed using prefix deduplication and negative integer references",
            "encoding": "Negative integers (-1, -2, ...) represent compressed paths",
            "decoding": "path_id = (-value) - 1; entry = path_legend.paths[path_id]; full_path = path_legend.prefixes[entry[0]] + entry[1]",
            "example": "-5 -> path_id=4 -> paths[4]=[1,'main.py'] -> prefixes[1]='src/' -> 'src/main.py'"
        },
        
        "3_enum_encoding": {
            "what": "Repeated string values replaced with integer indices",
            "fields": ["parse_status", "param_kind"],
            "decoding": "enums[field_name][integer_value]",
            "example": "parse_status=0 with enums.parse_status=['ok','error'] -> 'ok'"
        },
        
        "4_type_deduplication": {
            "what": "Type hint strings deduplicated across entire document",
            "decoding": "type_strings[integer_value]",
            "example": "type_hint=3 with type_strings=['str','int','bool','List[str]'] -> 'List[str]'"
        },
        
        "5_nested_schemas": {
            "what": "Complex fields have their own schema/legend/rows structure",
            "fields": [
                "functions_detailed",
                "classes_detailed",
                "globals_detailed",
                "crosstalk_candidates_py_v1",
                "crosstalk_candidates_ts_v1"
            ],
        },
    },
    
    "decoding_algorithm": {
        "step_1": "Load schema array to determine field order",
        "step_2": "Load legend dict to map abbreviations to full field names",
        "step_3": "For each row array: map positions to field names using schema[i] -> legend[abbr]",
        "step_4": "Decode values based on field type:",
        "step_4a": "  - Negative integers in ANY field: decode as path via path_legend",
        "step_4b": "  - Non-negative integers in path fields (file, node_id, imports): decode as path via path_legend",
        "step_4c": "  - Integers in enum fields (parse_status, param_kind): lookup in enums array",
        "step_4d": "  - Integers in type_hint fields: lookup in type_strings array",
        "step_4e": "  - All other values: use as-is",
        "step_5": "For nested schemas: repeat steps 1-4 recursively"
    },
    
    "field_types": {
        "path_fields": {
            "description": "Fields containing file path strings or arrays of paths",
            "fields": ["file", "node_id", "imports", "imports_all_raw"],
            "encoding": "Non-negative integers are path IDs, negative integers are path IDs (encoded differently), strings are preserved",
            "decoding": "If integer >= 0: direct path lookup. If integer < 0: path_id = (-value) - 1 then lookup"
        },
        
        "enum_fields": {
            "description": "Fields with limited set of string values",
            "fields": ["parse_status", "param_kind"],
            "encoding": "String -> index in enum array",
            "decoding": "enums[field_name][value]"
        },
        
        "type_fields": {
            "description": "Fields containing type hint strings",
            "fields": ["type_hint", "return_type"],
            "encoding": "String -> index in type_strings array",
            "decoding": "type_strings[value]"
        },
        
        "nested_schema_fields": {
            "description": "Fields containing nested compressed structures (language-specific variants may exist)",
            "fields": ["functions_detailed", "classes_detailed", "globals_detailed", "crosstalk_candidates_py_v1", "crosstalk_candidates_ts_v1"],
            "structure": "Each has own schema, legend, and rows/data"
        },

        "language_structural_fields": {
            "description": "Language-specific declaration fields (primarily Kotlin/JVM languages)",
            "fields": [
                "interfaces",
                "objects",
                "enums",
                "type_aliases",
                "exports",
                "declared_types",
                "declared_types_fq",
                "public_exports"
            ],
            "meaning": "Represent structural declarations at the file level; used for symbol graph construction and cross-language analysis"
        },

        "core_graph_fields": {
            "scc_id": {
                "type": "string",
                "meaning": "Identifier of the strongly connected component (cycle group) this node belongs to"
            },
            "scc_size": {
                "type": "integer",
                "meaning": "Number of nodes in the SCC; >1 indicates participation in a cycle"
            }
        },
        "optional_node_fields": {
            "description": "Optional fields present only when the analyzer has the data",
            "scc_id_runtime": {
                "type": "string (path-encoded) | absent",
                "meaning": "SCC component id computed from runtime-only imports (excludes TYPE_CHECKING-gated imports). Matches scc_id when the conceptual and runtime SCCs agree; differs when TYPE_CHECKING imports are the only reason a node is in a larger conceptual cycle.",
                "absent_when": "Bundle pre-dates dual-SCC support, or node is not in any runtime cycle (scc_size_runtime would be 1)"
            },
            "scc_size_runtime": {
                "type": "integer | absent",
                "meaning": "Size of the runtime SCC this node belongs to. Value of 1 means the node is not in any runtime cycle. Compare to scc_size (conceptual) to identify nodes whose cycle membership is TYPE_CHECKING-only.",
                "absent_when": "Bundle pre-dates dual-SCC support"
            }
        }
    },

    "summary_structure": {
        "cycles": {
            "description": "Cycle (SCC) metrics for the dependency graph",
            "always_present": {
                "cycle_nodes": "Total number of nodes participating in any non-trivial SCC (conceptual graph)",
                "largest_scc_size": "Size of the largest SCC in the conceptual graph (runtime + TYPE_CHECKING imports)"
            },
            "present_when_dual_scc_enabled": {
                "scc_conceptual": {
                    "description": "SCC metrics from the full import graph (runtime + TYPE_CHECKING imports). Matches per-node scc_id/scc_size.",
                    "fields": {
                        "cycle_nodes": "Nodes in non-trivial SCCs",
                        "largest_scc_size": "Largest SCC size",
                        "scc_count": "Number of distinct non-trivial SCCs"
                    }
                },
                "scc_runtime": {
                    "description": "SCC metrics from runtime-only imports (TYPE_CHECKING imports excluded). Reflects true import-system cycle risk — cycles Python's import machinery can actually encounter.",
                    "fields": {
                        "cycle_nodes": "Nodes in non-trivial runtime SCCs",
                        "largest_scc_size": "Largest runtime SCC size",
                        "scc_count": "Number of distinct non-trivial runtime SCCs"
                    }
                },
                "tc_inflation": {
                    "type": "integer",
                    "description": "scc_conceptual.largest_scc_size minus scc_runtime.largest_scc_size. Measures how much TYPE_CHECKING imports inflate the apparent cycle size. A large value indicates TYPE_CHECKING is being used to suppress architectural coupling rather than for its legitimate purposes (forward references, optional dependencies). Zero means the conceptual and runtime cycle structures are identical."
                }
            }
        }
    },
    
    "complete_example": {
        "compressed_node_row": [0, 0, "main", 100, 0, [272, 568]],
        "context": {
            "schema": ["f", "id", "n", "loc", "ps", "imp"],
            "legend": {"f": "file", "id": "node_id", "n": "name", "loc": "loc", "ps": "parse_status", "imp": "imports"},
            "enums": {"parse_status": ["ok", "error", "partial"]},
            "path_legend": {
                "prefixes": ["src/", "tests/"],
                "paths": [[0, "main.py"], [0, "utils.py"], [1, "test_main.py"]]
            }
        },
        "decoding_process": [
            "Position 0: value=0, field='file' (from schema/legend), decode as path: paths[0]=[0,'main.py'] -> 'src/main.py'",
            "Position 1: value=0, field='node_id', decode as path: 'src/main.py'",
            "Position 2: value='main', field='name', string -> 'main'",
            "Position 3: value=100, field='loc', number -> 100",
            "Position 4: value=0, field='parse_status' (enum field), decode: parse_status[0] -> 'ok'",
            "Position 5: value=[272,568], field='imports' (path array), decode each: [paths[272], paths[568]]"
        ],
        "decoded_result": {
            "file": "src/main.py",
            "node_id": "src/main.py",
            "name": "main",
            "loc": 100,
            "parse_status": "ok",
            "imports": ["<decoded_path_272>", "<decoded_path_568>"]
        }
    },
    
    "quick_reference": {
        "decode_field_name": "legend[schema[position]]",
        "decode_path_from_negative": "path_id = (-value - 1); full = prefixes[paths[path_id][0]] + paths[path_id][1]",
        "decode_path_from_nonnegative": "full = prefixes[paths[value][0]] + paths[value][1]",
        "decode_enum": "enums[field_name][value]",
        "decode_type": "type_strings[value]",
        "decode_row": "dict(zip([legend[s] for s in schema], row))",
        "interpret_scc_metrics": "summary.cycles.tc_inflation > 0 means TYPE_CHECKING imports inflate the cycle count; summary.cycles.scc_runtime shows the true runtime risk; node.scc_size_runtime vs node.scc_size reveals per-node cycle membership difference"
    }
}


def get_format_guide():
    """
    Get the format guide for inclusion in compressed JSON.
    
    Returns:
        Format guide dictionary (~1.3KB when serialized)
    """
    return FORMAT_GUIDE