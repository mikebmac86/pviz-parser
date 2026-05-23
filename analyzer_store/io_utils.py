# analyzer_store/io_utils.py
from __future__ import annotations
"""
Small, shared utilities for the analyzer_store package.

Keep this module dependency-light and focused on JSON persistence and timestamps.
"""

from datetime import datetime, timezone
from hashlib import blake2s
from pathlib import Path
from typing import Any, Mapping

# Prefer orjson when available; fall back to stdlib json.
try:
    import orjson  # type: ignore
except Exception:  # pragma: no cover
    orjson = None  # type: ignore

import json
import os

__all__ = [
    "iso_utc",
    "blake2s_short",
    "_dumps_bytes",
    "read_json",
    "load_json_bytes",
    "atomic_write_json",
]

# ---------------------------------------------------------------------------
# Time / hashing
# ---------------------------------------------------------------------------

def iso_utc(dt: datetime | float | int | None = None) -> str:
    """Return an ISO-8601 UTC string with seconds precision and trailing 'Z'.

    Accepts:
      • datetime (any tz) → converted to UTC
      • unix timestamp (float|int) → interpreted as seconds since epoch
      • None → now()
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    elif isinstance(dt, (float, int)):
        dt = datetime.fromtimestamp(float(dt), tz=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def blake2s_short(data: bytes, *, prefix: str = "b2s:", n_hex: int = 8) -> str:
    """Short, stable content id for files/blobs (hex slice)."""
    if n_hex < 2:
        n_hex = 2
    return f"{prefix}{blake2s(data).hexdigest()[:n_hex]}"

# ---------------------------------------------------------------------------
# JSON encode/decode helpers (bytes-oriented, fast paths)
# ---------------------------------------------------------------------------

def _dumps_bytes(obj: Any, *, pretty: bool = True) -> bytes:
    """
    Return serialized JSON as bytes.
    - Uses orjson with pretty+sorted keys when available
    - Falls back to json.dumps + UTF-8 encode
    - Ensures trailing newline in pretty mode for friendlier diffs
    """
    if orjson is not None:
        opt = 0
        if pretty:
            # 2-space indent, sorted keys, and newline at EOF
            opt |= (
                orjson.OPT_INDENT_2
                | orjson.OPT_SORT_KEYS
                | orjson.OPT_APPEND_NEWLINE
            )
        return orjson.dumps(obj, option=opt)
    # Fallback: stdlib json
    text = json.dumps(obj, indent=2 if pretty else None, sort_keys=pretty)
    if pretty and not text.endswith("\n"):
        text += "\n"
    return text.encode("utf-8")


def read_json(path: Path) -> Any:
    """
    Strict JSON load from disk (raises on read/parse errors).
    - Reads raw bytes
    - orjson.loads when available, else json.loads
    """
    p = Path(path)
    data = p.read_bytes()
    if orjson is not None:
        return orjson.loads(data)
    return json.loads(data.decode("utf-8", errors="ignore"))


def load_json_bytes(path: Path) -> Any:
    """
    Tolerant JSON load from disk (never raises; returns {} on failure).
    - Reads raw bytes
    - orjson.loads when available; else json.loads with UTF-8 + errors='ignore'
    """
    try:
        data = Path(path).read_bytes()
    except Exception:
        return {}
    try:
        if orjson is not None:
            return orjson.loads(data)
        return json.loads(data.decode("utf-8", errors="ignore"))
    except Exception:
        return {}

# ---------------------------------------------------------------------------
# JSON persistence (atomic write)
# ---------------------------------------------------------------------------

def atomic_write_json(payload: Mapping[str, Any], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(_dumps_bytes(payload, pretty=True))
    os.replace(tmp, path)

