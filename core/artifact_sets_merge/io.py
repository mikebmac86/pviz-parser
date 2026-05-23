from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple


def load_json_any(p: Path) -> Any:
    return json.loads(p.read_text(encoding="utf-8"))


def write_json(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def find_first_existing(root: Optional[Path], candidates: tuple[str, ...]) -> Optional[Path]:
    if root is None:
        return None
    for rel in candidates:
        p = root / rel
        if p.exists():
            return p
    return None


def load_discovered_inputs(
    discovered: Mapping[str, Mapping[str, Optional[Path]]]
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str]]:
    loaded: Dict[str, Dict[str, Any]] = {}
    load_errors: Dict[str, str] = {}

    for lang, paths in discovered.items():
        loaded[lang] = {}
        for artifact_kind, p in paths.items():
            if p is None or not p.exists():
                loaded[lang][artifact_kind] = None
                continue
            try:
                loaded[lang][artifact_kind] = load_json_any(p)
            except Exception as e:
                loaded[lang][artifact_kind] = None
                load_errors[str(p)] = repr(e)

    return loaded, load_errors


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except Exception:
        return default