from __future__ import annotations

"""
Go analyzer package.

This package currently provides:
  - FolderIndex builder for Go files (module-id space = Go package import path)
  - Lightweight symbol parser for per-file NodeFacts population

Wiring strategy (recommended):
  1) Build Go FolderIndex alongside Python/TS FolderIndex (or merge into one).
  2) Reuse the existing NodeFacts builder (language-agnostic) which will parse
     symbols via a dispatcher that calls parse_go_file for *.go.
  3) EdgePass remains unchanged (edges derived from NodeFacts.imports).
"""

from .go_folder_index import build_folder_index, save_folder_index, load_folder_index
from .go_parse import parse_go_file, GoParsedFile

__all__ = [
    "build_folder_index",
    "save_folder_index",
    "load_folder_index",
    "parse_go_file",
    "GoParsedFile",
]
