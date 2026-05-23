#saas_analyzer/analyzer/java/config.py
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class JavaAnalyzerCfg:
    """
    Config for Java analyzer (Phase 1: FolderIndex + symbols).

    Philosophy:
      - Keep config surface minimal and stable
      - Mirror GoAnalyzerCfg / TSAnalyzerCfg where concepts overlap
      - Reflect Java-specific conventions (packages, tests, generated sources)
    """

    # ------------------------------------------------------------------
    # File discovery (mostly informational; BUCKETS already selects *.java)
    # ------------------------------------------------------------------
    include_globs: List[str] = field(default_factory=lambda: [
        "**/*.java",
    ])

    exclude_globs: List[str] = field(default_factory=lambda: [
        "**/.git/**",
        "**/target/**",          # Maven
        "**/build/**",           # Gradle / general
        "**/out/**",             # IDE output
        "**/.gradle/**",
        "**/.idea/**",
        "**/.cache/**",
        "**/.pviz_store/**",
    ])

    # ------------------------------------------------------------------
    # Size / safety limits
    # ------------------------------------------------------------------
    # Canonical name used by some analyzers (kept for cross-lang symmetry).
    # FolderIndex currently checks max_file_bytes first, then falls back.
    max_file_bytes: int = 200_000_000  # 200 MB

    # Back-compat alias (some callers/configs may still set this).
    # Keep in sync with max_file_bytes if you use both externally.
    max_bytes_per_file: int = 200_000_000  # 200 MB

    # ------------------------------------------------------------------
    # Java-specific behavior toggles
    # ------------------------------------------------------------------
    include_tests: bool = False
    include_generated: bool = False
    respect_module_info: bool = True

    # If True, collapse edges/nodes to package-level IDs (only if your edge/node builders honor it)
    package_level_graph: bool = True

    # ------------------------------------------------------------------
    # Artifact output filenames (match canonical exporter expectations)
    # ------------------------------------------------------------------
    nodefacts_name: str = "nodefacts.json"
    edges_name: str = "edges.json"
    folder_index_name: str = "folder_index_java.json"

    # ------------------------------------------------------------------
    # Reserved for future expansion (kept for symmetry / forwards-compat)
    # ------------------------------------------------------------------
    enable_javac: bool = False

    # Prefer a string for subprocess invocation consistency (matches PVIZ_JAVA_BIN behavior).
    # If you keep Path instead, ensure the runner converts to str.
    java_bin: str = "java"

    # Optional: explicit classpath roots (Phase 2+)
    classpath: Optional[List[Path]] = None
