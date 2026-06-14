from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class RubyAnalyzerCfg:
    """
    Package-side configuration for the Ruby/Rails analyzer.

    Discovery/filtering and external parser invocation settings live here.
    Artifact generation, edge projection, and folder-index construction are
    controlled by the later-stage fields below.
    """

    # -----------------------------------------------------------------------
    # File selection
    # -----------------------------------------------------------------------
    include_globs: List[str] = field(default_factory=lambda: [
        "**/*.rb",
        "**/*.rake",
        "**/*.gemspec",
        "**/*.ru",
        "Gemfile",
        "Rakefile",
        "Capfile",
        "Guardfile",
        # config.ru and Vagrantfile are valid Ruby files processed by pviz.
        "config.ru",
        "Vagrantfile",
    ])

    exclude_globs: List[str] = field(default_factory=lambda: [
        "**/.git/**",
        "**/.bundle/**",
        "**/vendor/bundle/**",   # bundled gems only — not all of vendor/
        "**/tmp/**",
        "**/log/**",
        "**/coverage/**",
        "**/.cache/**",
        "**/.pviz_store/**",
        "**/node_modules/**",
        "**/generated/**",
    ])

    max_file_bytes: int = 200_000_000
    max_bytes_per_file: int = 200_000_000

    include_tests: bool = False
    include_generated: bool = False

    # -----------------------------------------------------------------------
    # External parser invocation
    # -----------------------------------------------------------------------
    rubyparser_cli_path: Optional[Path] = None
    rubyparser_timeout_s: int = 120

    # Options forwarded into ruby_analysis_request@v1 "options".
    # The Ruby CLI currently acts only on max_bytes_per_file; the rest are
    # stored for when selective extraction is implemented.
    rails_mode: str = "auto"   # "auto" | "on" | "off"
    include_constant_refs: bool = True
    include_method_calls: bool = True
    include_rails_dsl: bool = True
    include_bundler_index: bool = True
    include_dynamic_require_facts: bool = True

    # -----------------------------------------------------------------------
    # Artifact naming (matches Kotlin convention)
    # -----------------------------------------------------------------------
    nodefacts_name: str = "nodefacts.json"
    edges_name: str = "edges.json"
    folder_index_name: str = "folder_index_ruby.json"

    # -----------------------------------------------------------------------
    # Edge / graph projection
    # -----------------------------------------------------------------------
    # Minimum CallIndex confidence to emit a call edge.
    call_edge_min_confidence: float = 0.6
    # Gate for emitting resolved target-candidate call edges at all.
    ruby_include_candidate_edges: bool = True
    # Gate for emitting method-call edges specifically.
    ruby_include_method_call_edges: bool = True
    # Include Rails association edges (belongs_to / has_many / etc.)
    include_rails_association_edges: bool = True
    # Include cross-file constant-reference edges (lower confidence than calls).
    include_constant_ref_edges: bool = False
    # Emit edges to external (non-repo) files.
    ruby_include_external_edges: bool = False

    # -----------------------------------------------------------------------
    # Runtime caches (populated by ruby_run.py after analysis)
    # -----------------------------------------------------------------------
    ruby_analysis_cache: Optional[Any] = None
    ruby_parse_cache: Dict[str, Any] = field(default_factory=dict)

    # Populated by ruby_folder_index after declaration resolution.
    ruby_fq_decl_to_file: Dict[str, str] = field(default_factory=dict)
    ruby_module_to_files: Dict[str, List[str]] = field(default_factory=dict)

    # Raw index dicts from the Ruby parser handoff.
    ruby_constant_index: Dict[str, Any] = field(default_factory=dict)
    ruby_method_index: Dict[str, Any] = field(default_factory=dict)
    ruby_call_index: Dict[str, Any] = field(default_factory=dict)
    ruby_require_index: Dict[str, Any] = field(default_factory=dict)
    ruby_rails_index: Dict[str, Any] = field(default_factory=dict)