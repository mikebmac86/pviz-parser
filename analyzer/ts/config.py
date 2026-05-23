#analyzer/ts/config.py
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class TSAnalyzerCfg:
    """
    Config for TS/JS Tree-sitter analyzer.
    Keeps things simple: you can expand later (tsconfig alias support, etc.)
    without changing artifact schema.
    """
    include_globs: List[str] = field(default_factory=lambda: [
        "**/*.ts", "**/*.tsx",
        "**/*.js", "**/*.jsx",
        "**/*.mjs", "**/*.cjs",
    ])

    exclude_globs: List[str] = field(default_factory=lambda: [
        "**/.git/**",
        "**/node_modules/**",
        "**/dist/**",
        "**/build/**",
        "**/.next/**",
        "**/out/**",
        "**/coverage/**",
        "**/.turbo/**",
        "**/.cache/**",
    ])

    # Optional: cap file size to prevent pathological parses
    max_bytes_per_file: int = 200_000_000  # 200 MB

    # If True, skip *.d.ts (often noisy for dependency graphs)
    skip_d_ts: bool = True

    # Tree-sitter language bundle path (optional).
    # If None, runtime will try tree_sitter_languages first.
    language_bundle_path: Optional[Path] = None

    # How to treat absolute imports like "/foo/bar"
    treat_slash_root_as_repo_root: bool = True

    # Emit edges only for resolved internal files (recommended).
    emit_external_edges: bool = False  # v1: keep externals in folder_index/import lists

    # Artifact output filenames (so you match your existing exporter)
    nodefacts_name: str = "nodefacts.json"
    edges_name: str = "edges.json"
    folder_index_name: str = "folder_index.json"
