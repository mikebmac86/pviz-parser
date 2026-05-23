from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class KotlinAnalyzerCfg:
    include_globs: List[str] = field(default_factory=lambda: ["**/*.kt", "**/*.kts"])
    exclude_globs: List[str] = field(default_factory=lambda: [
        "**/.git/**", "**/target/**", "**/build/**", "**/out/**", "**/.gradle/**",
        "**/.idea/**", "**/.cache/**", "**/.pviz_store/**", "**/generated/**",
        "**/build/generated/**",
    ])

    max_file_bytes: int = 200_000_000
    max_bytes_per_file: int = 200_000_000

    include_tests: bool = False
    include_kts: bool = True
    include_generated: bool = False

    kotlin_node_id_space: str = "file"  # "file" first; "package" later once maps are stable.
    kotlinparser_cli_path: Optional[Path] = None
    kotlinparser_timeout_s: int = 120

    nodefacts_name: str = "nodefacts.json"
    edges_name: str = "edges.json"
    folder_index_name: str = "folder_index_kotlin.json"

    kotlin_parse_cache: Optional[Dict[str, Any]] = None
    kotlin_file_id_to_package: Dict[str, str] = field(default_factory=dict)
    kotlin_fq_decl_to_file: Dict[str, str] = field(default_factory=dict)
    kotlin_package_to_files: Dict[str, List[str]] = field(default_factory=dict)

    kotlin_include_external_edges: bool = False
