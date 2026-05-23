from __future__ import annotations

"""
Rust parser engine using rustparser_cli.

Uses syn crate via CLI for accurate parsing, following Java's javaparser_cli pattern.
"""

import json
import subprocess
import shutil
from pathlib import Path
from typing import Optional

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


class RustParser:
    """
    Rust parser using rustparser_cli (similar to javaparser_cli).
    
    Uses syn crate via CLI for accurate parsing.
    """
    
    def __init__(self, cli_path: Optional[Path] = None):
        """
        Initialize parser with path to rustparser_cli executable.
        
        Args:
            cli_path: Path to rustparser_cli binary. If None, searches environment and PATH.
        """
        if cli_path:
            self.cli_path = cli_path
        else:
            import os
            
            # Priority 1: Environment variable (production) ← ADDED
            env_path = os.environ.get('PVIZ_RUSTPARSER_BIN')
            if env_path:
                env_path_obj = Path(env_path)
                if env_path_obj.exists():
                    self.cli_path = env_path_obj
                    return
            
            # Priority 2: Look for rustparser_cli in PATH
            found = shutil.which("rustparser_cli")
            if found:
                self.cli_path = Path(found)
            else:
                # Priority 3: Try relative to this file (development)
                # Try relative to this file: analyzer/rust/parse_rust/engine.py
                # Go up to saas_arch root, then to tools/rustparser_cli
                # Path: backend/saas_analyzer/analyzer/rust/parse_rust/engine.py
                # Navigate: ../../../../.. -> saas_arch root
                try:
                    analyzer_root = Path(__file__).parent.parent.parent.parent.parent.parent
                    tools_dir = analyzer_root / "tools" / "rustparser_cli"
                    
                    # Try release build first, then debug
                    if (tools_dir / "target" / "release" / "rustparser_cli").exists():
                        self.cli_path = tools_dir / "target" / "release" / "rustparser_cli"
                    elif (tools_dir / "target" / "debug" / "rustparser_cli").exists():
                        self.cli_path = tools_dir / "target" / "debug" / "rustparser_cli"
                    else:
                        self.cli_path = None
                except Exception:
                    self.cli_path = None
    
    def is_available(self) -> bool:
        """Check if rustparser_cli is available."""
        return self.cli_path is not None and self.cli_path.exists()
    
    def parse_file(self, file_path: Path, content: Optional[str] = None) -> RustParsedFile:
        """
        Parse a single Rust file using rustparser_cli.
        
        Returns RustParsedFile with ok=True/False and extracted symbols.
        """
        if not self.is_available():
            return RustParsedFile(
                file_path=file_path,
                ok=False,
                parse_status="no_cli",
                error="rustparser_cli not available"
            )
        
        try:
            # Call rustparser_cli with file path
            # Expected output: JSON with AST data
            result = subprocess.run(
                [str(self.cli_path), str(file_path)],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            
            if result.returncode != 0:
                return RustParsedFile(
                    file_path=file_path,
                    ok=False,
                    parse_status="cli_error",
                    error=result.stderr[:200] if result.stderr else "unknown error"
                )
            
            # Parse JSON output
            try:
                data = json.loads(result.stdout)
            except json.JSONDecodeError as e:
                return RustParsedFile(
                    file_path=file_path,
                    ok=False,
                    parse_status="json_error",
                    error=f"Failed to parse JSON: {e}"
                )
            
            # Convert JSON to RustParsedFile
            return self._json_to_parsed_file(file_path, data)
            
        except subprocess.TimeoutExpired:
            return RustParsedFile(
                file_path=file_path,
                ok=False,
                parse_status="timeout",
                error="Parse timeout after 30s"
            )
        except Exception as e:
            return RustParsedFile(
                file_path=file_path,
                ok=False,
                parse_status="exception",
                error=str(e)[:200]
            )
    
    def _json_to_parsed_file(self, file_path: Path, data: dict) -> RustParsedFile:
        """Convert JSON output from CLI to RustParsedFile."""
        result = RustParsedFile(
            file_path=file_path,
            ok=data.get("ok", False),
            parse_status=data.get("parse_status", "unknown"),
        )
        
        if not result.ok:
            result.error = data.get("error")
            return result
        
        # File type
        result.is_lib = data.get("is_lib", False)
        result.is_main = data.get("is_main", False)
        result.is_mod = data.get("is_mod", False)
        result.has_macro_use = data.get("has_macro_use", False)
        
        # Module path
        result.module_path = data.get("module_path")
        
        # Use statements
        for use_data in data.get("use_statements", []):
            result.use_statements.append(RustUseStatement(
                path=use_data["path"],
                alias=use_data.get("alias"),
                is_glob=use_data.get("is_glob", False),
                is_pub=use_data.get("is_pub", False),
            ))
        
        # Mod declarations
        for mod_data in data.get("mod_declarations", []):
            result.mod_declarations.append(RustModDeclaration(
                name=mod_data["name"],
                is_pub=mod_data.get("is_pub", False),
                is_inline=mod_data.get("is_inline", False),
            ))
        
        # Functions
        for fn_data in data.get("functions", []):
            result.functions.append(RustFunction(
                name=fn_data["name"],
                is_pub=fn_data.get("is_pub", False),
                is_async=fn_data.get("is_async", False),
                params=fn_data.get("params", []),
                return_type=fn_data.get("return_type"),
            ))
        
        # Structs
        for struct_data in data.get("structs", []):
            result.structs.append(RustStruct(
                name=struct_data["name"],
                is_pub=struct_data.get("is_pub", False),
                fields=struct_data.get("fields", []),
            ))
        
        # Enums
        for enum_data in data.get("enums", []):
            result.enums.append(RustEnum(
                name=enum_data["name"],
                is_pub=enum_data.get("is_pub", False),
                variants=enum_data.get("variants", []),
            ))
        
        # Traits
        for trait_data in data.get("traits", []):
            result.traits.append(RustTrait(
                name=trait_data["name"],
                is_pub=trait_data.get("is_pub", False),
                methods=trait_data.get("methods", []),
            ))
        
        # Impls
        for impl_data in data.get("impls", []):
            result.impls.append(RustImpl(
                target=impl_data["target"],
                trait_name=impl_data.get("trait_name"),
                methods=impl_data.get("methods", []),
            ))
        
        return result


# ---------------------------------------------------------------------------
# Helper functions (similar to Java's extract_* functions)
# ---------------------------------------------------------------------------

def extract_module_path(parsed: RustParsedFile) -> Optional[str]:
    """Extract module path from parsed file."""
    return parsed.module_path


def extract_use_statements(parsed: RustParsedFile) -> list[RustUseStatement]:
    """Extract use statements from parsed file."""
    return parsed.use_statements


def extract_declared_types(parsed: RustParsedFile) -> list[str]:
    """
    Extract all declared type names (structs, enums, traits).
    
    Similar to Java's extract_declared_types.
    """
    types = []
    types.extend([s.name for s in parsed.structs])
    types.extend([e.name for e in parsed.enums])
    types.extend([t.name for t in parsed.traits])
    return types


def extract_public_items(parsed: RustParsedFile) -> list[str]:
    """
    Extract public API surface (pub items).
    
    Similar to Java's public exports extraction.
    """
    public = []
    public.extend([s.name for s in parsed.structs if s.is_pub])
    public.extend([e.name for e in parsed.enums if e.is_pub])
    public.extend([t.name for t in parsed.traits if t.is_pub])
    public.extend([f.name for f in parsed.functions if f.is_pub])
    return public


def parse_rust_file(file_path: Path, cli_path: Optional[Path] = None) -> RustParsedFile:
    """
    Convenience function to parse a single Rust file.
    
    Similar to parse_java_file() in Java analyzer.
    
    Args:
        file_path: Path to .rs file
        cli_path: Optional path to rustparser_cli binary
    
    Returns:
        RustParsedFile with parsed symbols
    """
    parser = RustParser(cli_path=cli_path)
    return parser.parse_file(file_path)