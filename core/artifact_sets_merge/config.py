from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

EXT_TO_LANG: Dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".rs": "rust",
}

_CODE_EXTS = set(EXT_TO_LANG)


@dataclass(frozen=True)
class LanguageSpec:
    lang: str
    key: str
    fallback_dir: str
    nodefacts_candidates: Tuple[str, ...]
    edges_candidates: Tuple[str, ...]
    folder_index_candidates: Tuple[str, ...]
    reachable_candidates: Tuple[str, ...] = ("reachable.json",)


LANGUAGE_SPECS: Tuple[LanguageSpec, ...] = (
    LanguageSpec(
        lang="python",
        key="py",
        fallback_dir="analyzers/python",
        nodefacts_candidates=("nodefacts.json",),
        edges_candidates=("edges.json", "classic/edges/full.json"),
        folder_index_candidates=("folder_index.json",),
    ),
    LanguageSpec(
        lang="ts",
        key="ts",
        fallback_dir="analyzers/ts",
        nodefacts_candidates=("nodefacts.json",),
        edges_candidates=("edges.json",),
        folder_index_candidates=("folder_index.json",),
    ),
    LanguageSpec(
        lang="go",
        key="go",
        fallback_dir="analyzers/go",
        nodefacts_candidates=("nodefacts.json",),
        edges_candidates=("edges_go.json", "edges.json"),
        folder_index_candidates=("folder_index_go.json", "folder_index.json"),
    ),
    LanguageSpec(
        lang="java",
        key="java",
        fallback_dir="analyzers/java",
        nodefacts_candidates=("nodefacts.json",),
        edges_candidates=("edges_java.json", "edges.json"),
        folder_index_candidates=("folder_index_java.json", "folder_index.json"),
    ),
    LanguageSpec(
        lang="kotlin",
        key="kotlin",
        fallback_dir="analyzers/kotlin",
        nodefacts_candidates=("nodefacts.json",),
        edges_candidates=("edges_kotlin.json", "edges.json"),
        folder_index_candidates=("folder_index_kotlin.json", "folder_index.json"),
    ),
    LanguageSpec(
        lang="rust",
        key="rust",
        fallback_dir="analyzers/rust",
        nodefacts_candidates=("nodefacts.json",),
        edges_candidates=("edges_rust.json", "edges.json"),
        folder_index_candidates=("folder_index_rust.json", "folder_index.json"),
    ),
)