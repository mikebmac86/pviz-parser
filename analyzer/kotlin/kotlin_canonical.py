from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Set


@dataclass(frozen=True)
class ResolveResult:
    kind: str
    spec: str
    resolved: Optional[str]
    reason: str = ""


# Prefixes that unambiguously identify stdlib / platform imports.
# Rule: any import whose first segment matches one of these (with a trailing dot
# to avoid false-positives on user packages like "kotlinApp.") is external.
KOTLIN_DEFAULT_IMPORT_PREFIXES = (
    # Kotlin stdlib
    "kotlin.",
    "kotlin.collections.",
    "kotlin.comparisons.",
    "kotlin.io.",
    "kotlin.ranges.",
    "kotlin.sequences.",
    "kotlin.text.",
    "kotlin.math.",
    "kotlin.reflect.",
    "kotlin.time.",
    # Kotlin extensions (coroutines, serialization, etc.)
    "kotlinx.",
    # JVM stdlib
    "java.",
    "javax.",
    "sun.",
    "com.sun.",
    # Android / AndroidX
    "android.",
    "androidx.",
    # JetBrains annotations (common transitive dep)
    "org.jetbrains.",
    # Kotlin compiler internals (show up in annotation processors)
    "org.jetbrains.kotlin.",
)

# Fast-reject set: bare top-level names that are unambiguously external
# even without a trailing dot (avoids the prefix loop for common cases).
_STDLIB_BARE = frozenset({"kotlin", "java.lang", "kotlinx", "android", "androidx"})


def normalize_kotlin_import_spec(path: str, *, is_wildcard: bool = False) -> str:
    base = (path or "").strip()
    if not base:
        return ""
    if base.endswith(".*"):
        base = base[:-2]
        is_wildcard = True
    return f"{base}.*" if is_wildcard else base


def is_default_or_stdlib_import(spec: str) -> bool:
    """
    Return True if spec is unambiguously stdlib / platform and should never
    resolve to an internal file.

    Guards against false-positives on user packages that share a prefix with
    stdlib (e.g. "kotlinApp.utils" must NOT match "kotlin.").
    """
    s = (spec or "").strip()
    # Strip wildcard suffix for prefix matching
    if s.endswith(".*"):
        s = s[:-2]
    if not s:
        return False
    if s in _STDLIB_BARE:
        return True
    # Prefix match: require the spec to start with "<prefix>" where the prefix
    # already ends in "." — so "kotlin.collections.List" matches "kotlin." but
    # "kotlinApp.Foo" does NOT match "kotlin." (no false positive).
    return any(s.startswith(prefix) for prefix in KOTLIN_DEFAULT_IMPORT_PREFIXES)


def fq_decl(package_name: Optional[str], simple: str) -> Optional[str]:
    pkg = (package_name or "").strip()
    name = (simple or "").strip()
    if not name:
        return None
    return f"{pkg}.{name}" if pkg else name


def resolve_kotlin_import(
    spec: str,
    *,
    fq_decl_to_file: Dict[str, str],
    package_to_files: Dict[str, Set[str]],
) -> List[ResolveResult]:
    """
    Resolve a normalized import spec to zero or more ResolveResults.

    Resolution order:
      1. Empty / invalid spec -> []
      2. Stdlib / platform prefix -> external (reason="default_or_stdlib")
      3. Wildcard import (ends with ".*"):
           - package known internally -> internal results (reason="wildcard_package")
           - package unknown          -> external (reason="package_not_found")
      4. Direct FQ type match in fq_decl_to_file -> internal (reason="direct")
      5. Owner-decl walk (strips last segment repeatedly):
           - match found -> internal (reason="owner_decl")
      6. No match -> external (reason="decl_not_found")
    """
    s = normalize_kotlin_import_spec(spec)
    if not s:
        return []

    # Fast-path: stdlib / platform
    if is_default_or_stdlib_import(s):
        return [ResolveResult(kind="external", spec=s, resolved=None, reason="default_or_stdlib")]

    # Wildcard package import
    if s.endswith(".*"):
        pkg = s[:-2]
        files = package_to_files.get(pkg)
        if files:
            return [
                ResolveResult(kind="internal", spec=s, resolved=fid, reason="wildcard_package")
                for fid in sorted(files)
            ]
        return [ResolveResult(kind="external", spec=s, resolved=None, reason="package_not_found")]

    # Direct type lookup
    file_id = fq_decl_to_file.get(s)
    if file_id:
        return [ResolveResult(kind="internal", spec=s, resolved=file_id, reason="direct")]

    # Owner-decl walk — only walk if the spec has enough segments to plausibly
    # be a nested type or static member (at least 3 segments: pkg.Type.member).
    # This avoids walking obviously-external short specs like "retrofit2.Call".
    segments = s.split(".")
    if len(segments) >= 3:
        cur = s
        while "." in cur:
            cur = cur.rsplit(".", 1)[0]
            file_id2 = fq_decl_to_file.get(cur)
            if file_id2:
                return [ResolveResult(kind="internal", spec=s, resolved=file_id2, reason="owner_decl")]

    return [ResolveResult(kind="external", spec=s, resolved=None, reason="decl_not_found")]