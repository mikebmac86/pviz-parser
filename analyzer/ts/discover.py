#analyzer/ts/discover.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Set


@dataclass(frozen=True)
class DiscoveredFile:
    abs_path: Path
    rel_posix: str  # repo-relative, POSIX separators


def _matches_any(path: Path, globs: Iterable[str]) -> bool:
    # Path.match uses POSIX-style patterns even on Windows for many cases;
    # still safest if we apply to POSIX form.
    p = Path(path.as_posix())
    for g in globs:
        if p.match(g):
            return True
    return False


def discover_files(*, repo_root: Path, include_globs: List[str], exclude_globs: List[str]) -> List[DiscoveredFile]:
    repo_root = repo_root.resolve()

    seen: Set[Path] = set()
    out: List[DiscoveredFile] = []

    # We’ll walk once and filter; this is faster than many glob calls on huge repos.
    for abs_path in repo_root.rglob("*"):
        if not abs_path.is_file():
            continue

        rel = abs_path.relative_to(repo_root)
        if _matches_any(rel, exclude_globs):
            continue
        if not _matches_any(rel, include_globs):
            continue

        if abs_path in seen:
            continue
        seen.add(abs_path)

        out.append(
            DiscoveredFile(
                abs_path=abs_path,
                rel_posix=rel.as_posix(),
            )
        )

    out.sort(key=lambda d: d.rel_posix)
    return out
