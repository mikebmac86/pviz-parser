from __future__ import annotations

"""
Rust parser submodule.

Provides rustparser_cli-backed parsing with clean API.
"""

from .models import (
    RustParsedFile,
    RustUseStatement,
    RustModDeclaration,
    RustFunction,
    RustStruct,
    RustEnum,
    RustTrait,
    RustImpl,
)

from .engine import (
    parse_rust_file,
    extract_module_path,
    extract_use_statements,
    extract_declared_types,
    extract_public_items,
)

__all__ = [
    # Models
    "RustParsedFile",
    "RustUseStatement",
    "RustModDeclaration",
    "RustFunction",
    "RustStruct",
    "RustEnum",
    "RustTrait",
    "RustImpl",
    # Functions
    "parse_rust_file",
    "extract_module_path",
    "extract_use_statements",
    "extract_declared_types",
    "extract_public_items",
]