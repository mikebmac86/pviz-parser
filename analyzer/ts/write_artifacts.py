#analyzer/ts/write_artifacts.py
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


def write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=False)
