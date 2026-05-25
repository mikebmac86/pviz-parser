# analyzer/ts/parser_runtime.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import sys


class TSRuntimeError(RuntimeError):
    pass


@dataclass
class Parsers:
    javascript: Any
    typescript: Any
    tsx: Any


class TSRuntime:
    """
    Runtime that provides parsers for JS/TS/TSX using tree-sitter-language-pack,
    which returns real tree_sitter.Parser instances (no capsule mismatch).
    """

    def __init__(self) -> None:
        try:
            import tree_sitter_language_pack as language_pack  # type: ignore
            from tree_sitter_language_pack import get_parser  # type: ignore
        except Exception as e:
            raise TSRuntimeError(
                "Failed to import tree-sitter-language-pack.\n"
                "Install/check with:\n"
                "  python -m pip install tree-sitter-language-pack\n"
                f"Python executable: {sys.executable}\n"
                f"Original error: {type(e).__name__}: {e}"
            ) from e

        package_path = getattr(language_pack, "__file__", "<unknown>")

        try:
            javascript = get_parser("javascript")
        except Exception as e:
            raise TSRuntimeError(
                "tree-sitter-language-pack imported, but JavaScript parser initialization failed.\n"
                f"Python executable: {sys.executable}\n"
                f"Package path: {package_path}\n"
                f"Original error: {type(e).__name__}: {e}"
            ) from e

        try:
            typescript = get_parser("typescript")
        except Exception as e:
            raise TSRuntimeError(
                "tree-sitter-language-pack imported, but TypeScript parser initialization failed.\n"
                f"Python executable: {sys.executable}\n"
                f"Package path: {package_path}\n"
                f"Original error: {type(e).__name__}: {e}"
            ) from e

        try:
            tsx = get_parser("tsx")
        except Exception as e:
            raise TSRuntimeError(
                "tree-sitter-language-pack imported, but TSX parser initialization failed.\n"
                f"Python executable: {sys.executable}\n"
                f"Package path: {package_path}\n"
                f"Original error: {type(e).__name__}: {e}"
            ) from e

        self._parsers = Parsers(
            javascript=javascript,
            typescript=typescript,
            tsx=tsx,
        )

    def parse(self, *, rel_path: str | Path, source_bytes: bytes):
        p = Path(rel_path)
        suf = p.suffix.lower()

        if suf == ".tsx":
            parser = self._parsers.tsx
        elif suf == ".ts":
            parser = self._parsers.typescript
        elif suf in (".js", ".jsx", ".mjs", ".cjs"):
            parser = self._parsers.javascript
        else:
            raise TSRuntimeError(f"Unknown TS/JS extension for parser routing: {p}")

        # tree-sitter-language-pack >= 1.x requires str, not bytes.
        # Byte offsets (start_byte, end_byte) remain valid on the returned
        # nodes, so callers can continue to slice source_bytes directly.
        return parser.parse(source_bytes.decode("utf-8", errors="replace"))