# i_o/diagnostics_export.py
from __future__ import annotations
from pathlib import Path
from typing import Mapping, Iterable, Sequence, Any, Optional
import json
import csv
import tempfile
import os

def _atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, encoding=encoding, dir=str(path.parent)) as tmp:
        tmp.write(text)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_name = tmp.name
    os.replace(tmp_name, path)

def write_json(path: Path, obj: Mapping[str, Any], *, indent: int = 2) -> None:
    _atomic_write_text(Path(path), json.dumps(obj, indent=indent))

def write_ndjson(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    lines = []
    for r in rows:
        lines.append(json.dumps(r))
    _atomic_write_text(Path(path), "\n".join(lines) + ("\n" if lines else ""))

def write_csv(path: Path, rows: Iterable[Mapping[str, Any]], *, fieldnames: Optional[Sequence[str]] = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", newline="", dir=str(path.parent)) as tmp:
        if rows is None:
            rows = []
        rows = list(rows)
        if not fieldnames and rows:
            # infer union of keys preserving first-row order first
            fieldnames = list(rows[0].keys())
            seen = set(fieldnames)
            for r in rows[1:]:
                for k in r.keys():
                    if k not in seen:
                        fieldnames.append(k)
                        seen.add(k)
        writer = csv.DictWriter(tmp, fieldnames=fieldnames or [])
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
        tmp.flush(); os.fsync(tmp.fileno())
        tmp_name = tmp.name
    os.replace(tmp_name, path)
