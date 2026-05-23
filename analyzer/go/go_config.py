# backend/saas_analyzer/analyzer/go/config.py
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class GoAnalyzerCfg:
    """
    Config for Go analyzer.

    Goals:
      - Backwards compatible with current FolderIndex expectations
      - Explicit knobs for Go batch AST extraction (stdlib go/parser helper)
      - Explicit knobs for richer Go-only sidecar artifacts (trim at merge)
      - Minimal but complete surface (avoid "magic env-only" behavior)
    """

    # ------------------------------------------------------------------
    # File discovery / filtering
    # ------------------------------------------------------------------
    # BUCKETS usually selects *.go already, but keep for symmetry / direct runs.
    include_globs: List[str] = field(default_factory=lambda: ["**/*.go"])

    exclude_globs: List[str] = field(
        default_factory=lambda: [
            "**/.git/**",
            "**/.hg/**",
            "**/.svn/**",
            "**/vendor/**",  # Go vendoring
            "**/dist/**",
            "**/build/**",
            "**/out/**",
            "**/.cache/**",
            "**/.pviz_store/**",
            "**/.pviz/**",
            "**/node_modules/**",
        ]
    )

    # Include *_test.go files in analysis
    include_tests: bool = True

    # Include files marked as generated (// Code generated … DO NOT EDIT.)
    # If False, generated files should be skipped where feasible.
    include_generated: bool = False

    # ------------------------------------------------------------------
    # Size / safety limits
    # ------------------------------------------------------------------
    # NOTE: FolderIndex currently reads cfg.max_file_bytes (legacy field name).
    # Keep both names; max_file_bytes is the canonical one for analyzers.
    max_file_bytes: int = 200_000_000  # 200 MB (matches TS default)
    # Alias for older code / readability. Prefer max_file_bytes going forward.
    max_bytes_per_file: int = 200_000_000

    # If True, allow parsing even when file exceeds max_file_bytes (not recommended)
    allow_oversize_files: bool = False

    # ------------------------------------------------------------------
    # Module / graph behavior
    # ------------------------------------------------------------------
    # Treat module path from go.mod as authoritative (monorepo aware via folder_index)
    respect_go_mod: bool = True

    # If True, collapse to package-level nodes (Phase 1 default).
    # If False, you may later emit file-level nodes/edges (Phase 2+).
    package_level_graph: bool = True

    # Include external edges in edges.json (internal-only is usually preferred for bundle merge)
    include_external_edges: bool = False

    # ------------------------------------------------------------------
    # Go AST batch extractor (recommended path)
    # ------------------------------------------------------------------
    # Enable batch extraction via Go helper (go/parser/go/ast).
    enable_go_ast: bool = True

    # Path to compiled extractor binary. If None, wrapper may fall back to:
    #   - env PVIZ_GOEXTRACT_BIN
    #   - default relative path (e.g., tools/goextract/goextract)
    goextract_bin: Optional[Path] = None

    # Timeout for a single batch invocation
    goextract_timeout_s: int = 180

    # Whether to include doc comments in extracted symbol records (can be large)
    goextract_include_docs: bool = True

    # Whether to include import records (alias/blank/dot + positions)
    goextract_include_imports: bool = True

    # Whether to include build constraints records (//go:build, // +build)
    goextract_include_build: bool = True

    # Maximum files per batch invocation (0/None means "single batch for all")
    # Useful if you see memory spikes on huge repos.
    goextract_batch_size: int = 0

    # ------------------------------------------------------------------
    # Artifact output filenames (match canonical exporter expectations)
    # ------------------------------------------------------------------
    nodefacts_name: str = "nodefacts.json"
    edges_name: str = "edges.json"
    folder_index_name: str = "folder_index_go.json"

    # Go-only sidecar artifacts (trim during merge)
    emit_go_details: bool = True
    go_details_name: str = "go_details.json"          # package rollups + rich symbols
    go_edge_reasons_name: str = "go_edge_reasons.json" # optional: edge->reasons index

    # Include rich symbol records (kind, receiver, line/col, docs) in go_details
    go_details_include_symbols: bool = True

    # Include per-file import resolution telemetry in go_details
    # (kind, resolved, tried chain) derived from canonical_go.resolve_go_import
    go_details_include_import_resolution: bool = True

    # Include build/platform flags (cgo, go:build, filename os/arch) in go_details
    go_details_include_build_flags: bool = True

    # Attach edge reasons (source_file, import form, tried chain) for internal edges
    emit_edge_reasons: bool = True

    # Cap reasons per (src,dst) edge to avoid huge payloads
    max_reasons_per_edge: int = 50

    # ------------------------------------------------------------------
    # Reserved / future expansion
    # ------------------------------------------------------------------
    # Optional: future go list / type-checking integration
    enable_go_list: bool = False

    # Optional: path to Go toolchain (if not on PATH)
    go_binary: Optional[Path] = None

    # ------------------------------------------------------------------
    # Small compatibility shim(s)
    # ------------------------------------------------------------------
    def __post_init__(self) -> None:
        # Keep aliases in sync if caller sets one but not the other.
        # Prefer max_file_bytes as canonical, but mirror to max_bytes_per_file.
        if self.max_file_bytes != self.max_bytes_per_file:
            # If one looks default and the other is overridden, converge.
            # Heuristic: if max_bytes_per_file is default but max_file_bytes changed, copy.
            if self.max_bytes_per_file == 200_000_000 and self.max_file_bytes != 200_000_000:
                self.max_bytes_per_file = self.max_file_bytes
            # Or vice versa.
            elif self.max_file_bytes == 200_000_000 and self.max_bytes_per_file != 200_000_000:
                self.max_file_bytes = self.max_bytes_per_file
            else:
                # If both were intentionally set differently, force canonical.
                self.max_bytes_per_file = self.max_file_bytes
