from __future__ import annotations

from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence

from .config import LANGUAGE_SPECS, LanguageSpec
from .io import find_first_existing


def resolve_optional_dir(p: Optional[Path]) -> Optional[Path]:
    return p.resolve() if p else None


def discover_language_inputs(
    *,
    artifacts_root: Path,
    set_dirs: Mapping[str, Optional[Path]],
    specs: Sequence[LanguageSpec] = LANGUAGE_SPECS,
) -> Dict[str, Dict[str, Optional[Path]]]:
    discovered: Dict[str, Dict[str, Optional[Path]]] = {}

    for spec in specs:
        set_dir = set_dirs.get(spec.lang)
        fallback_root = artifacts_root / spec.fallback_dir

        paths: Dict[str, Optional[Path]] = {
            "nodefacts": find_first_existing(set_dir, spec.nodefacts_candidates),
            "edges": find_first_existing(set_dir, spec.edges_candidates),
            "folder_index": find_first_existing(set_dir, spec.folder_index_candidates),
            "reachable": find_first_existing(set_dir, spec.reachable_candidates),
        }

        if paths["nodefacts"] is None:
            paths["nodefacts"] = find_first_existing(fallback_root, spec.nodefacts_candidates)
        if paths["edges"] is None:
            paths["edges"] = find_first_existing(fallback_root, spec.edges_candidates)
        if paths["folder_index"] is None:
            paths["folder_index"] = find_first_existing(fallback_root, spec.folder_index_candidates)
        if paths["reachable"] is None:
            paths["reachable"] = find_first_existing(fallback_root, spec.reachable_candidates)

        discovered[spec.lang] = paths

    return discovered