# pviz_parser/__init__.py
"""
pviz-parser: Codebase dependency analysis — structured bundles with nodes,
edges, metrics, and cycle detection across multiple languages.

Public API:
    build_llm_bundle_headless   — full pipeline: analyze + export bundle
    main                        — CLI entry point (pviz command)
"""
from __future__ import annotations

from pviz_parser.cli import main
from core.json_export import build_llm_bundle_headless
from analyzer.config import AnalyzerCfg
__all__ = [
    "main",
    "build_llm_bundle_headless",
    "AnalyzerCfg",
]

__version__ = "0.1.4"