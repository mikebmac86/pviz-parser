from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .errors import LLMJsonExportError


def load_json(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            v = json.load(f)
            return v if isinstance(v, dict) else {"_raw": v}
    except FileNotFoundError as exc:
        raise LLMJsonExportError(f"Required artifact not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise LLMJsonExportError(f"Invalid JSON in artifact: {path}") from exc


def load_json_optional(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            v = json.load(f)
            return v if isinstance(v, dict) else {"_raw": v}
    except json.JSONDecodeError as exc:
        raise LLMJsonExportError(f"Invalid JSON in optional artifact: {path}") from exc


def find_artifact(root: Path, *candidates: str) -> Path:
    tried: List[Path] = []
    for rel in candidates:
        p = root / rel
        tried.append(p)
        if p.exists():
            return p

    parent = root.parent
    if parent and parent != root:
        for rel in candidates:
            p = parent / rel
            tried.append(p)
            if p.exists():
                return p

    paths_str = " | ".join(str(p) for p in tried)
    raise LLMJsonExportError(f"Required artifact not found (tried: {paths_str})")


def find_optional_artifact(root: Path, *candidates: str) -> Optional[Path]:
    for rel in candidates:
        p = root / rel
        if p.exists():
            return p

    parent = root.parent
    if parent and parent != root:
        for rel in candidates:
            p = parent / rel
            if p.exists():
                return p

    return None
