# i_o/json_compression/__init__.py
"""
i_o/json_compression

Router / re-export layer for the json_compression submodule.

Public API:
  - apply_schema_encoding
  - decode_schema
"""

from __future__ import annotations

from .run import apply_schema_encoding, decode_schema

__all__ = ["apply_schema_encoding", "decode_schema"]
