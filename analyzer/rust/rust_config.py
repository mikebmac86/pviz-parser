from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class RustAnalyzerCfg:
    """
    Config for Rust analyzer (Phase 1: FolderIndex + symbols).

    Philosophy:
      - Keep config surface minimal and stable
      - Mirror JavaAnalyzerCfg / GoAnalyzerCfg / TSAnalyzerCfg where concepts overlap
      - Reflect Rust-specific conventions (crates, workspaces, tests, target dir)
    """

    # ------------------------------------------------------------------
    # File discovery (mostly informational; BUCKETS already selects *.rs)
    # ------------------------------------------------------------------
    include_globs: List[str] = field(default_factory=lambda: [
        "**/*.rs",
    ])

    exclude_globs: List[str] = field(default_factory=lambda: [
        "**/.git/**",
        "**/target/**",          # Cargo build output
        "**/.cargo/**",          # Cargo cache
        "**/.idea/**",           # IDE
        "**/.vscode/**",         # IDE
        "**/.cache/**",
        "**/.pviz_store/**",
    ])

    # ------------------------------------------------------------------
    # Size / safety limits
    # ------------------------------------------------------------------
    # Canonical name used by some analyzers (kept for cross-lang symmetry).
    # FolderIndex currently checks max_file_bytes first, then falls back.
    max_file_bytes: int = 200_000_000  # 200 MB

    # Back-compat alias (some callers/configs may still set this).
    # Keep in sync with max_file_bytes if you use both externally.
    max_bytes_per_file: int = 200_000_000  # 200 MB

    # ------------------------------------------------------------------
    # Rust-specific behavior toggles
    # ------------------------------------------------------------------
    include_tests: bool = False
    include_benches: bool = False
    include_examples: bool = False

    # Handle Cargo workspaces (multiple crates)
    respect_workspace: bool = True

    # If True, collapse edges/nodes to crate-level IDs (only if your edge/node builders honor it)
    crate_level_graph: bool = False

    # ------------------------------------------------------------------
    # Rust parser configuration
    # ------------------------------------------------------------------
    # Path to rustparser_cli binary (if None, searches PATH or tools/rustparser_cli)
    rustparser_cli_path: Optional[Path] = None

    # Parallelization settings
    max_workers: Optional[int] = None  # None = use cpu_count()

    # Optional override used by NodeFacts symbols extraction parallel path
    # (build_artifacts.py may use this separately from max_workers).
    rust_max_workers: Optional[int] = None

    # ------------------------------------------------------------------
    # NodeFacts/Edges builder behavior (build_artifacts.py)
    # ------------------------------------------------------------------
    # Node ID space for rust artifacts: "file" (default) or "module"
    rust_node_id_space: str = "file"

    # NodeFacts symbols extraction strategy:
    #   - "dispatcher" (default): use parse_symbols_for_nodefacts(Path|RustParsedFile,...)
    #   - "none": disable symbol parsing (forces file-id space)
    rust_nodefacts_symbols: str = "dispatcher"

    # Additive per-node enrichment from parse cache (traits/impls/derives/attrs/type_aliases/mod_decls)
    rust_nodefacts_enrichment: bool = True

    # Additive edge enrichment from parse cache (impl->trait, supertraits, derives, optional attrs/mod decls)
    rust_edges_enrichment: bool = False
    rust_edges_mod_decls: bool = False
    rust_edges_attributes: bool = False

    # ------------------------------------------------------------------
    # FolderIndex semantic options
    # ------------------------------------------------------------------
    # If True, treat `mod foo;` (non-inline) declarations as internal edges during FolderIndex build.
    # Default True to preserve the current FolderIndex behavior in your provided file.
    rust_include_mod_decl_edges_internal: bool = True

    # ------------------------------------------------------------------
    # Diagnostics / debug
    # ------------------------------------------------------------------
    # Enables DIAGNOSTIC ONLY prints in folder_index.py (also gated by env PVIZ_RUST_DIAG)
    rust_diag: bool = False

    # ------------------------------------------------------------------
    # Artifact output filenames (match canonical exporter expectations)
    # ------------------------------------------------------------------
    nodefacts_name: str = "nodefacts.json"
    edges_name: str = "edges.json"
    folder_index_name: str = "folder_index_rust.json"

    # ------------------------------------------------------------------
    # Reserved for future expansion (kept for symmetry / forwards-compat)
    # ------------------------------------------------------------------
    # Parse Cargo.toml for dependency information
    enable_cargo_toml: bool = True

    # Cargo binary path for workspace detection
    cargo_bin: str = "cargo"

    # Optional: explicit Cargo.toml path for workspace root
    cargo_toml_path: Optional[Path] = None

    # Parse cache (internal use - populated by folder_index builder)
    rust_parse_cache: Optional[dict] = None

    # Repo root (internal use - populated by folder_index builder; used for cache lookups / abs keys)
    repo_root: Optional[str] = None

    # Published maps (internal use - populated by folder_index builder)
    rust_module_to_file: Optional[dict] = None
    rust_crate_to_files: Optional[dict] = None
    rust_file_id_to_module: Optional[dict] = None
