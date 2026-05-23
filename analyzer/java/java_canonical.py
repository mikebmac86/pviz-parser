#saas_analyzer/analyzer/java/canonical_java.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

@dataclass(frozen=True)
class ResolveResult:
    kind: str                 # "internal" | "external"
    spec: str                 # normalized spec
    resolved: Optional[str]   # internal file_id when internal
    reason: str = ""

def normalize_java_import_spec(target: str, *, is_wildcard: bool, is_static: bool) -> str:
    """
    Represent wildcard as "<pkg>.*" and static as "static:<spec>".
    
    Examples:
        normalize_java_import_spec("io.kestra.cli.App", is_wildcard=False, is_static=False)
        -> "io.kestra.cli.App"
        
        normalize_java_import_spec("io.kestra.cli", is_wildcard=True, is_static=False)
        -> "io.kestra.cli.*"
        
        normalize_java_import_spec("io.kestra.core.utils.Rethrow.sneaky", is_wildcard=False, is_static=True)
        -> "static:io.kestra.core.utils.Rethrow.sneaky"
    """
    base = (target or "").strip()
    if not base:
        return ""
    if is_wildcard:
        base = base + ".*"
    if is_static:
        base = "static:" + base
    return base

# ---------------------------------------------------------------------------
# Java-specific canonicalization helpers
# ---------------------------------------------------------------------------

def derive_package_from_path(file_path: str) -> Optional[str]:
    """
    Derive Java package from file path by extracting the path after /src/main/java/ or /src/test/java/.
    
    Examples:
        "kestra-develop/cli/src/main/java/io/kestra/cli/AbstractCommand.java"
        -> "io.kestra.cli"
        
        "kestra-develop/core/src/test/java/io/kestra/core/utils/TestUtils.java"
        -> "io.kestra.core.utils"
    
    Returns:
        Package name or None if path doesn't follow standard Maven structure
    """
    for java_src in ['/src/main/java/', '/src/test/java/']:
        if java_src in file_path:
            after_java = file_path.split(java_src, 1)[1]
            package_path = after_java.rsplit('/', 1)[0]
            return package_path.replace('/', '.')
    return None

def derive_classname_from_path(file_path: str) -> Optional[str]:
    """
    Extract simple class name from file path.
    
    Examples:
        "kestra-develop/cli/src/main/java/io/kestra/cli/AbstractCommand.java"
        -> "AbstractCommand"
    """
    if file_path.endswith('.java'):
        return file_path.rsplit('/', 1)[1][:-5]  # Remove .java extension
    return None

def build_fq_typename(package: str, classname: str, inner_class: Optional[str] = None) -> str:
    """
    Build fully-qualified type name.
    
    Examples:
        build_fq_typename("io.kestra.cli", "AbstractCommand")
        -> "io.kestra.cli.AbstractCommand"
        
        build_fq_typename("io.kestra.cli", "AbstractCommand", "LogLevel")
        -> "io.kestra.cli.AbstractCommand.LogLevel"
    """
    if not package or not classname:
        return ""
    fq = f"{package}.{classname}"
    if inner_class:
        fq = f"{fq}.{inner_class}"
    return fq

def canonicalize_java_nodefacts(nodefacts: dict) -> dict:
    """
    Enhance Java nodefacts with derived package information and FQ names.
    
    This function:
    1. Derives package from file path (if not already present)
    2. Builds proper FQ names for declared_types_fq
    3. Preserves all existing data
    
    Args:
        nodefacts: Raw nodefacts dict from parser
    
    Returns:
        Enhanced nodefacts with package and FQ name information
    """
    enhanced_nodes = {}
    
    for file_id, node in nodefacts.get('nodes', {}).items():
        enhanced = dict(node)  # Copy existing data
        
        # Derive package if not present
        if not enhanced.get('package'):
            derived_pkg = derive_package_from_path(file_id)
            if derived_pkg:
                enhanced['package'] = derived_pkg
        
        package = enhanced.get('package')
        
        # Build proper FQ names for declared types
        if package and 'declared_types' in enhanced:
            fq_names = []
            for dtype in enhanced['declared_types']:
                # Handle inner classes (e.g., "AbstractCommand.LogLevel")
                if '.' in dtype and not dtype.startswith(package):
                    # This is likely ClassName.InnerClass format
                    parts = dtype.split('.', 1)
                    fq_name = build_fq_typename(package, parts[0], parts[1])
                else:
                    # Simple class name
                    fq_name = build_fq_typename(package, dtype)
                
                if fq_name:
                    fq_names.append(fq_name)
            
            enhanced['declared_types_fq'] = fq_names
        
        enhanced_nodes[file_id] = enhanced
    
    result = dict(nodefacts)
    result['nodes'] = enhanced_nodes
    return result

def build_type_to_file_mapping(nodefacts: dict) -> Dict[str, str]:
    """
    Build mapping from FQ type name to file_id.
    
    This is required for resolve_java_import() to work.
    
    Examples:
        {
            "io.kestra.cli.AbstractCommand": "kestra-develop/cli/.../AbstractCommand.java",
            "io.kestra.cli.AbstractCommand.LogLevel": "kestra-develop/cli/.../AbstractCommand.java",
            "io.kestra.core.plugins.PluginManager": "kestra-develop/core/.../PluginManager.java"
        }
    
    Args:
        nodefacts: Enhanced nodefacts (after canonicalize_java_nodefacts)
    
    Returns:
        Mapping from FQ type name to file_id
    """
    type_to_file = {}
    
    for file_id, node in nodefacts.get('nodes', {}).items():
        for fq_type in node.get('declared_types_fq', []):
            if fq_type:
                type_to_file[fq_type] = file_id
    
    return type_to_file

def build_package_to_files_mapping(nodefacts: dict) -> Dict[str, Set[str]]:
    """
    Build mapping from package name to set of file_ids in that package.
    
    This is required for wildcard import resolution.
    
    Examples:
        {
            "io.kestra.cli": {
                "kestra-develop/cli/.../AbstractCommand.java",
                "kestra-develop/cli/.../App.java",
                ...
            },
            "io.kestra.core.plugins": {
                "kestra-develop/core/.../PluginManager.java",
                ...
            }
        }
    
    Args:
        nodefacts: Enhanced nodefacts (after canonicalize_java_nodefacts)
    
    Returns:
        Mapping from package name to set of file_ids
    """
    package_to_files = {}
    
    for file_id, node in nodefacts.get('nodes', {}).items():
        package = node.get('package')
        if package:
            if package not in package_to_files:
                package_to_files[package] = set()
            package_to_files[package].add(file_id)
    
    return package_to_files

def canonicalize_java_folder_index(folder_index: dict, nodefacts: dict) -> dict:
    """
    Enhance folder_index with resolved import information.
    
    This function:
    1. Converts file-path internal imports to FQ type names
    2. Adds metadata needed for import resolution
    
    Args:
        folder_index: Raw folder_index from parser
        nodefacts: Enhanced nodefacts (after canonicalize_java_nodefacts)
    
    Returns:
        Enhanced folder_index
    """
    # Build reverse mapping: file_id -> list of FQ types declared in that file
    file_to_types = {}
    for file_id, node in nodefacts.get('nodes', {}).items():
        types = node.get('declared_types_fq', [])
        if types:
            file_to_types[file_id] = types
    
    enhanced_files = {}
    
    for key, file_data in folder_index.get('files', {}).items():
        enhanced = dict(file_data)
        
        # Convert imports_internal (file paths) to resolved FQ type names
        # Note: imports_all already contains FQ names from the source code
        imports_internal_files = enhanced.get('imports_internal', [])
        
        # Store both formats for compatibility
        enhanced['imports_internal_files'] = imports_internal_files  # Original file paths
        
        # Resolve internal imports to their FQ type names
        resolved_types = []
        for internal_file in imports_internal_files:
            types = file_to_types.get(internal_file, [])
            resolved_types.extend(types)
        
        enhanced['imports_internal_resolved'] = resolved_types
        
        enhanced_files[key] = enhanced
    
    result = dict(folder_index)
    result['files'] = enhanced_files
    return result

# ---------------------------------------------------------------------------
# Import resolution (unchanged from original)
# ---------------------------------------------------------------------------

def resolve_java_import(
    spec: str,
    *,
    type_to_file: Dict[str, str],
    package_to_files: Dict[str, Set[str]],
) -> List[ResolveResult]:
    """
    spec is normalized from normalize_java_import_spec().
    Returns 0..N internal resolutions (wildcards may expand).
    Resolved value is a file_id (repo-relative posix path) in MVP.
    
    Examples:
        # Direct import
        resolve_java_import(
            "io.kestra.core.plugins.PluginManager",
            type_to_file={"io.kestra.core.plugins.PluginManager": "core/.../PluginManager.java"},
            package_to_files={}
        )
        -> [ResolveResult(kind="internal", spec="...", resolved="core/.../PluginManager.java")]
        
        # Wildcard import
        resolve_java_import(
            "io.kestra.cli.*",
            type_to_file={},
            package_to_files={"io.kestra.cli": {"cli/.../App.java", "cli/.../AbstractCommand.java"}}
        )
        -> [
            ResolveResult(kind="internal", spec="io.kestra.cli.*", resolved="cli/.../AbstractCommand.java"),
            ResolveResult(kind="internal", spec="io.kestra.cli.*", resolved="cli/.../App.java")
        ]
        
        # External import
        resolve_java_import(
            "java.util.List",
            type_to_file={},
            package_to_files={}
        )
        -> [ResolveResult(kind="external", spec="java.util.List", resolved=None, reason="type_not_found")]
    """
    s = (spec or "").strip()
    if not s:
        return []

    is_static = False
    if s.startswith("static:"):
        is_static = True
        s = s[len("static:") :]

    if s.endswith(".*"):
        pkg = s[:-2]
        files = package_to_files.get(pkg)
        if files:
            return [ResolveResult(kind="internal", spec=spec, resolved=fid) for fid in sorted(files)]
        return [ResolveResult(kind="external", spec=spec, resolved=None, reason="package_not_found")]

    # direct type import
    file_id = type_to_file.get(s)
    if file_id:
        return [ResolveResult(kind="internal", spec=spec, resolved=file_id)]

    # static: treat as depending on owning type if present
    # (our parser hands base type already; if not, best-effort: strip last segment)
    if is_static:
        # If someone fed "com.Foo.BAR" (shouldn't with our parser), try "com.Foo"
        if "." in s:
            owner = s.rsplit(".", 1)[0]
            file_id2 = type_to_file.get(owner)
            if file_id2:
                return [ResolveResult(kind="internal", spec=spec, resolved=file_id2)]

    return [ResolveResult(kind="external", spec=spec, resolved=None, reason="type_not_found")]
