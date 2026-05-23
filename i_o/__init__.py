from __future__ import annotations
"""
I/O utilities for PViz (headless/CLI)

Exports:
  • Graph JSON serialization (stable schema)
  • Layout persistence (positions + zoom)
  • Path utilities (ensure_extension, unique_path)
  • Workspace I/O + manager (JSON/TOML)
"""

# -----------------------------------------------------------------------------
# Headless-safe I/O
# -----------------------------------------------------------------------------

# layout
from .layout_store import save_layout_to_disk, load_layout_from_disk, LayoutIOError

# json graph
from .export_json import (
    serialize_graph_to_json_dict,
    export_graph_json,
    load_graph_json,
    GraphJSONError,
)

# paths
from .path_utils import ensure_extension, unique_path

# workspace/settings I/O
from .workspace_io import (
    WorkspacePaths,
    make_workspace_paths,
    read_json,
    write_json,
    read_toml,
    WorkspaceIOError,
)
from .workspace_manager import WorkspaceManager


__all__ = [
    # layout
    "save_layout_to_disk", "load_layout_from_disk", "LayoutIOError",
    # json graph
    "serialize_graph_to_json_dict", "export_graph_json", "load_graph_json", "GraphJSONError",
    # paths
    "ensure_extension", "unique_path",
    # workspace
    "WorkspacePaths", "make_workspace_paths",
    "read_json", "write_json", "read_toml",
    "WorkspaceIOError", "WorkspaceManager",
]

__version__ = "0.3.0"