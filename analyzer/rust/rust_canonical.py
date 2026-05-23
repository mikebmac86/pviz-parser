from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List, Optional, Set
from pathlib import Path

@dataclass(frozen=True)
class ResolveResult:
    kind: str                 # "internal" | "external"
    spec: str                 # normalized spec
    resolved: Optional[str]   # internal file_id when internal
    reason: str = ""

def normalize_rust_use_path(path: str, *, is_glob: bool, is_pub: bool) -> str:
    """
    Represent glob as "<path>::*" and pub as "pub:<spec>".
    
    Examples:
        normalize_rust_use_path("std::collections::HashMap", is_glob=False, is_pub=False)
        -> "std::collections::HashMap"
        
        normalize_rust_use_path("std::collections", is_glob=True, is_pub=False)
        -> "std::collections::*"
        
        normalize_rust_use_path("crate::utils::helper", is_glob=False, is_pub=True)
        -> "pub:crate::utils::helper"
    """
    base = (path or "").strip()
    if not base:
        return ""
    if is_glob:
        base = base + "::*"
    if is_pub:
        base = "pub:" + base
    return base

# ---------------------------------------------------------------------------
# Rust-specific canonicalization helpers
# ---------------------------------------------------------------------------

def derive_module_from_path(file_path: str, repo_root: Path) -> Optional[str]:
    """
    Derive Rust module path from file path.
    
    Rust module structure:
      - src/lib.rs -> crate (root)
      - src/main.rs -> crate (root)
      - src/foo.rs -> crate::foo
      - src/foo/mod.rs -> crate::foo
      - src/foo/bar.rs -> crate::foo::bar
      - src/foo/bar/mod.rs -> crate::foo::bar
    
    Examples:
        "my-project/src/lib.rs" -> "crate"
        "my-project/src/main.rs" -> "crate"
        "my-project/src/utils.rs" -> "crate::utils"
        "my-project/src/utils/helper.rs" -> "crate::utils::helper"
        "my-project/src/utils/mod.rs" -> "crate::utils"
    
    Returns:
        Module path or None if path doesn't follow standard Rust structure
    """
    try:
        path = Path(file_path)
        
        # Find src/ directory
        parts = list(path.parts)
        if 'src' not in parts:
            return None
        
        src_idx = parts.index('src')
        after_src = parts[src_idx + 1:]
        
        if not after_src:
            return None
        
        # Handle lib.rs and main.rs as crate root
        if len(after_src) == 1 and after_src[0] in ('lib.rs', 'main.rs'):
            return "crate"
        
        # Build module path
        module_parts = []
        for i, part in enumerate(after_src):
            # Skip mod.rs (it represents the parent directory)
            if part == 'mod.rs':
                break
            
            # Remove .rs extension from files
            if part.endswith('.rs'):
                module_parts.append(part[:-3])
            else:
                # Directory name
                module_parts.append(part)
        
        if not module_parts:
            return "crate"
        
        return "crate::" + "::".join(module_parts)
        
    except Exception:
        return None

def derive_module_name_from_path(file_path: str) -> Optional[str]:
    """
    Extract simple module name from file path.
    
    Examples:
        "my-project/src/utils/helper.rs" -> "helper"
        "my-project/src/utils/mod.rs" -> "utils"
    """
    try:
        path = Path(file_path)
        
        if path.name == 'mod.rs':
            # Module name is parent directory
            return path.parent.name
        elif path.suffix == '.rs':
            # Module name is filename without extension
            return path.stem
        
        return None
    except Exception:
        return None

def build_module_path(crate_name: str, *components: str) -> str:
    """
    Build module path from components.
    
    Examples:
        build_module_path("my_crate", "utils", "helper")
        -> "my_crate::utils::helper"
        
        build_module_path("crate", "foo", "bar")
        -> "crate::foo::bar"
    """
    if not crate_name:
        return ""
    
    parts = [crate_name]
    for comp in components:
        if comp:
            parts.append(comp)
    
    return "::".join(parts)

def canonicalize_rust_nodefacts(nodefacts: dict) -> dict:
    """
    Enhance Rust nodefacts with derived module information and paths.
    
    This function:
    1. Derives module path from file path (if not already present)
    2. Preserves all existing data
    
    Args:
        nodefacts: Raw nodefacts dict from parser
    
    Returns:
        Enhanced nodefacts with module path information
    """
    enhanced_nodes = {}
    
    for file_id, node in nodefacts.get('nodes', {}).items():
        enhanced = dict(node)  # Copy existing data
        
        # Derive module path if not present
        if not enhanced.get('module_path'):
            # Need repo_root to derive module path properly
            # For now, just use file_id as-is
            # This will be enhanced when called from build_artifacts
            pass
        
        enhanced_nodes[file_id] = enhanced
    
    result = dict(nodefacts)
    result['nodes'] = enhanced_nodes
    return result

def build_module_to_file_mapping(nodefacts: dict) -> Dict[str, str]:
    """
    Build mapping from module path to file_id.
    
    This is required for resolve_rust_use() to work.
    
    Examples:
        {
            "crate::utils": "src/utils.rs",
            "crate::utils::helper": "src/utils/helper.rs",
            "crate::config": "src/config/mod.rs"
        }
    
    Args:
        nodefacts: Enhanced nodefacts (after canonicalize_rust_nodefacts)
    
    Returns:
        Mapping from module path to file_id
    """
    module_to_file = {}
    
    for file_id, node in nodefacts.get('nodes', {}).items():
        module_path = node.get('module_path')
        if module_path:
            module_to_file[module_path] = file_id
    
    return module_to_file

def build_crate_to_files_mapping(nodefacts: dict) -> Dict[str, Set[str]]:
    """
    Build mapping from crate name to set of file_ids in that crate.
    
    This is required for wildcard use resolution.
    
    Examples:
        {
            "crate": {
                "src/lib.rs",
                "src/utils.rs",
                "src/config/mod.rs",
                ...
            },
            "tokio": {
                # External crate - would be empty for internal analysis
            }
        }
    
    Args:
        nodefacts: Enhanced nodefacts (after canonicalize_rust_nodefacts)
    
    Returns:
        Mapping from crate name to set of file_ids
    """
    crate_to_files = {}
    
    for file_id, node in nodefacts.get('nodes', {}).items():
        module_path = node.get('module_path')
        if module_path:
            # Extract crate name (first component)
            if module_path.startswith("crate::"):
                crate_name = "crate"
            elif "::" in module_path:
                crate_name = module_path.split("::", 1)[0]
            else:
                crate_name = module_path
            
            if crate_name:
                if crate_name not in crate_to_files:
                    crate_to_files[crate_name] = set()
                crate_to_files[crate_name].add(file_id)
    
    return crate_to_files

def canonicalize_rust_folder_index(folder_index: dict, nodefacts: dict) -> dict:
    """
    Enhance folder_index with resolved use information.
    
    This function:
    1. Converts file-path internal uses to module paths
    2. Adds metadata needed for use resolution
    
    Args:
        folder_index: Raw folder_index from parser
        nodefacts: Enhanced nodefacts (after canonicalize_rust_nodefacts)
    
    Returns:
        Enhanced folder_index
    """
    # Build reverse mapping: file_id -> module path declared in that file
    file_to_module = {}
    for file_id, node in nodefacts.get('nodes', {}).items():
        module_path = node.get('module_path')
        if module_path:
            file_to_module[file_id] = module_path
    
    enhanced_files = {}
    
    for key, file_data in folder_index.get('files', {}).items():
        enhanced = dict(file_data)
        
        # Convert imports_internal (file paths) to resolved module paths
        # Note: imports_all already contains use paths from the source code
        imports_internal_files = enhanced.get('imports_internal', [])
        
        # Store both formats for compatibility
        enhanced['imports_internal_files'] = imports_internal_files  # Original file paths
        
        # Resolve internal uses to their module paths
        resolved_modules = []
        for internal_file in imports_internal_files:
            module_path = file_to_module.get(internal_file)
            if module_path:
                resolved_modules.append(module_path)
        
        enhanced['imports_internal_resolved'] = resolved_modules
        
        enhanced_files[key] = enhanced
    
    result = dict(folder_index)
    result['files'] = enhanced_files
    return result

# ---------------------------------------------------------------------------
# Use resolution
# ---------------------------------------------------------------------------

def resolve_rust_use(
    spec: str,
    *,
    module_to_file: Dict[str, str],
    crate_to_files: Dict[str, Set[str]],
) -> List[ResolveResult]:
    """
    spec is normalized from normalize_rust_use_path().
    Returns 0..N internal resolutions (globs may expand).
    Resolved value is a file_id (repo-relative posix path).
    
    Examples:
        # Direct use
        resolve_rust_use(
            "crate::utils::helper",
            module_to_file={"crate::utils::helper": "src/utils/helper.rs"},
            crate_to_files={}
        )
        -> [ResolveResult(kind="internal", spec="...", resolved="src/utils/helper.rs")]
        
        # Glob use
        resolve_rust_use(
            "crate::utils::*",
            module_to_file={"crate::utils::foo": "src/utils/foo.rs", "crate::utils::bar": "src/utils/bar.rs"},
            crate_to_files={"crate": {"src/utils/foo.rs", "src/utils/bar.rs"}}
        )
        -> [
            ResolveResult(kind="internal", spec="crate::utils::*", resolved="src/utils/bar.rs"),
            ResolveResult(kind="internal", spec="crate::utils::*", resolved="src/utils/foo.rs")
        ]
        
        # External use
        resolve_rust_use(
            "std::collections::HashMap",
            module_to_file={},
            crate_to_files={}
        )
        -> [ResolveResult(kind="external", spec="std::collections::HashMap", resolved=None, reason="module_not_found")]
    """
    s = (spec or "").strip()
    if not s:
        return []

    is_pub = False
    if s.startswith("pub:"):
        is_pub = True
        s = s[len("pub:"):]

    # Handle glob imports
    if s.endswith("::*"):
        module_prefix = s[:-3]
        
        # Find all modules that start with this prefix
        matching_files = set()
        for mod_path, file_id in module_to_file.items():
            if mod_path.startswith(module_prefix + "::") or mod_path == module_prefix:
                matching_files.add(file_id)
        
        if matching_files:
            return [ResolveResult(kind="internal", spec=spec, resolved=fid) for fid in sorted(matching_files)]
        
        return [ResolveResult(kind="external", spec=spec, resolved=None, reason="module_not_found")]

    # Direct module use
    file_id = module_to_file.get(s)
    if file_id:
        return [ResolveResult(kind="internal", spec=spec, resolved=file_id)]

    # Try to resolve parent module
    # e.g., "crate::utils::helper::function" -> try "crate::utils::helper"
    if "::" in s:
        parts = s.split("::")
        for i in range(len(parts), 0, -1):
            potential_module = "::".join(parts[:i])
            file_id2 = module_to_file.get(potential_module)
            if file_id2:
                return [ResolveResult(kind="internal", spec=spec, resolved=file_id2)]

    # Check if it's referencing a known crate (external)
    if "::" in s:
        crate_name = s.split("::", 1)[0]
        # Known external crates
        external_crates = {
            "std", "core", "alloc",  # Standard library
            "tokio", "serde", "async_trait", "futures",  # Common crates
        }
        if crate_name in external_crates or crate_name not in crate_to_files:
            return [ResolveResult(kind="external", spec=spec, resolved=None, reason="external_crate")]

    return [ResolveResult(kind="external", spec=spec, resolved=None, reason="module_not_found")]