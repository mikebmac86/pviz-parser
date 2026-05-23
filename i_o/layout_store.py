# i_o/layout_store.py
from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

# Prefer the shared ISO utility for consistency with analyzer_store.*
try:
    from analyzer_store.io_utils import iso_utc as _iso_utc  # consistent UTC format
except Exception:
    _iso_utc = None  # fallback below
try:
    import orjson  # fast path
except Exception:  # pragma: no cover
    orjson = None

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class LayoutIOError(Exception):
    """Raised when saving or loading a layout fails."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _loads_bytes_dict(b: bytes) -> Dict[str, Any]:
    """
    Tolerant JSON loader:
      - orjson fast-path when available
      - fallback to stdlib with UTF-8 decode(errors='ignore')
      - always return a dict ({} if shape mismatches)
    """
    try:
        obj = orjson.loads(b) if orjson else json.loads(b.decode("utf-8", errors="ignore"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}

def _atomic_write_json(payload: Mapping[str, Any], path: Path) -> None:
    """
    Atomic, durable-ish JSON write:
      - pretty, sorted keys, UTF-8, no ASCII escaping
      - writes to a temp file (prefer same directory), fsync, then replace
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)

    fd: Optional[int] = None
    tmp_path: Optional[str] = None
    try:
        # Prefer the target directory for true atomic replace
        try:
            fd, tmp_path = tempfile.mkstemp(
                prefix=path.name + ".",
                dir=str(path.parent),
            )
        except Exception:
            # Fallback: use system temp dir if target dir is not writable
            fd, tmp_path = tempfile.mkstemp(
                prefix=path.name + ".",
                dir=None,
            )

        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            fd = None  # fd now owned by the file object
            f.write(data)
            try:
                f.flush()
                os.fsync(f.fileno())
            except Exception:
                pass

        tmp = Path(tmp_path)
        # Replace into the final location (cross-dir is fine; just not strictly atomic)
        os.replace(tmp, path)

        # Best-effort directory fsync for durability
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            pass

    finally:
        # Cleanup temp file if it still exists and isn't the final path
        try:
            if tmp_path is not None:
                if os.path.exists(tmp_path) and os.path.abspath(tmp_path) != os.path.abspath(path):
                    os.remove(tmp_path)
        except Exception:
            pass


def _artifacts_dir_from_base(base: Path | str) -> Path:
    """
    Accepts:
      • <store_root>
      • <store_root>/.pviz
      • <store_root>/.pviz/artifacts
    Returns: <store_root>/.pviz/artifacts (created if missing)
    """
    p = Path(base).expanduser().resolve()
    if p.name == "artifacts" and p.parent.name == ".pviz":
        out = p
    elif p.name == ".pviz":
        out = (p / "artifacts").resolve()
    else:
        out = (p / ".pviz" / "artifacts").resolve()
    out.mkdir(parents=True, exist_ok=True)
    return out

def _zones_root(artifacts_dir: Path | str) -> Path:
    """
    Return '<artifacts>/zones' (created if missing).
    """
    root = Path(artifacts_dir).resolve() / "zones"
    root.mkdir(parents=True, exist_ok=True)
    return root

def _sanitize_zone_id(zone_id: str) -> str:
    s = str(zone_id or "").strip()
    bad = '<>:"\\|?*'
    tbl = str.maketrans({c: "-" for c in bad})
    s = s.translate(tbl).replace("/", "-").replace("\\", "-")
    while "--" in s:
        s = s.replace("--", "-")
    return s or "zone"

def _zone_dir_for(artifacts_dir: Path | str, zone_id: str) -> Path:
    return _zones_root(artifacts_dir) / _sanitize_zone_id(zone_id)


# ---------------------------------------------------------------------------
# Artifact cleanup
# ---------------------------------------------------------------------------
def _rm_tree(p: Path) -> bool:
    try:
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
            return True
    except Exception:
        pass
    return False

def clear_artifacts(root: Path, *, include_sessions: bool = True, include_zones: bool = True) -> int:
    """
    Robust wipe.
    Accepts either the artifacts dir (.../.pviz/artifacts) OR the store_root (.../.pviz_store).
    Returns count of directories removed.
    """
    removed = 0
    try:
        root = root.resolve()
    except Exception:
        pass

    # Determine store_root & artifacts_dir
    if root.name == "artifacts" and root.parent.name == ".pviz":
        store_root = root.parent.parent.parent
        artifacts_dir = root
    else:
        store_root = root
        artifacts_dir = store_root / ".pviz" / "artifacts"

    # 1) Main artifacts
    if _rm_tree(artifacts_dir):
        removed += 1

    # 2) Zones sub-tree (when artifacts_dir existed or was recreated later)
    if include_zones:
        if _rm_tree(artifacts_dir / "zones"):
            removed += 1

    # 3) Session-scoped artifacts
    if include_sessions:
        sessions = store_root / "_sessions"
        if sessions.exists():
            for sess in sessions.iterdir():
                if not sess.is_dir():
                    continue
                ad = sess / ".pviz" / "artifacts"
                if _rm_tree(ad):
                    removed += 1
    return removed
# ---------------------------------------------------------------------------
# Layout persistence
# ---------------------------------------------------------------------------

def save_layout_to_disk(
    positions: Mapping[str, Tuple[float, float]],
    zoom: float,
    path: Path,
) -> None:
    # Guard: by default, refuse to write into the scanned repo tree
    repo_root = os.environ.get("PVIZ_REPO_ROOT", "").strip()
    allow = str(os.environ.get("PVIZ_ALLOW_REPO_WRITES", "")).lower() in ("1", "true", "yes")
    if repo_root and str(Path(path).resolve()).startswith(str(Path(repo_root).resolve())) and not allow:
        raise LayoutIOError(f"Refusing to write layout into repo: {path}")

    pos_norm: Dict[str, list[float]] = {}
    for k, (x, y) in positions.items():
        try:
            pos_norm[str(k)] = [float(x), float(y)]
        except Exception:
            continue

    payload = {"positions": pos_norm, "zoom": float(zoom)}
    try:
        _atomic_write_json(payload, Path(path))
    except Exception as e:
        raise LayoutIOError(f"Failed to save layout to {path}: {e}") from e


def load_layout_from_disk(path: Path) -> tuple[Dict[str, Tuple[float, float]], float]:
    p = Path(path)
    if not p.exists():
        return {}, 1.0

    try:
        data = _loads_bytes_dict(p.read_bytes())
        if not data:
            raise ValueError("empty or non-dict JSON")
    except Exception as e:
        raise LayoutIOError(f"Failed to read layout from {path}: {e}") from e

    try:
        positions_raw = data.get("positions") or {}
        pos: Dict[str, Tuple[float, float]] = {}
        for k, v in positions_raw.items():
            if isinstance(v, (list, tuple)) and len(v) >= 2:
                pos[str(k)] = (float(v[0]), float(v[1]))
        zoom = float(data.get("zoom", 1.0))
    except Exception as e:
        raise LayoutIOError(f"Invalid layout format in {path}: {e}") from e

    return pos, zoom


# ---------------------------------------------------------------------------
# Pins I/O  (store_root-only; no silent CWD/repo fallbacks)
# ---------------------------------------------------------------------------

def _pins_path(base_dir: Path | str) -> Path:
    """
    Resolve the pins.json location **inside the sandbox** (store_root family).
    Accepts store_root, store_root/.pviz, or store_root/.pviz/artifacts.
    """
    if base_dir is None:
        raise LayoutIOError(
            "pins.json base_dir is required (must point to store_root, not the repo)."
        )
    return _artifacts_dir_from_base(base_dir) / "pins.json"


def save_pins_to_disk(
    pins: dict[str, dict],
    *,
    base_dir: Optional[Path] = None,
    path: Optional[Path] = None,
) -> None:
    """
    Persist pins to store_root. One of (path | base_dir) is required.
      • If 'path' is given, it's used directly.
      • Else we save to '<base_dir>/.pviz/artifacts/pins.json'.
    """
    if path is None and base_dir is None:
        raise LayoutIOError("save_pins_to_disk requires 'path' or 'base_dir' (store_root).")

    out = Path(path) if path is not None else _pins_path(Path(base_dir))
    tmp = out.with_suffix(".tmp")

    updated_at = _iso_utc() if callable(_iso_utc) else (datetime.utcnow().isoformat(timespec="seconds") + "Z")

    pins_norm: dict[str, dict] = {}
    for nid, rec in (pins or {}).items():
        if not isinstance(rec, dict):
            continue
        pins_norm[str(nid)] = {
            "pinned": bool(rec.get("pinned", False)),
            "x": rec.get("x"),
            "y": rec.get("y"),
        }

    data = {"version": 1, "updated_at": updated_at, "pins": pins_norm}

    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
        tmp.replace(out)
    finally:
        try:
            if tmp.exists():
                # Py3.8+: missing_ok available; keep portable
                tmp.unlink()
        except Exception:
            pass


def load_pins_from_disk(
    *,
    base_dir: Optional[Path] = None,
    path: Optional[Path] = None,
) -> dict[str, dict]:
    """
    Load pins from store_root. One of (path | base_dir) is required.
    """
    if path is None and base_dir is None:
        raise LayoutIOError("load_pins_from_disk requires 'path' or 'base_dir' (store_root).")

    p = Path(path) if path is not None else _pins_path(Path(base_dir))
    if not p.exists():
        return {}
    try:
        data = _loads_bytes_dict(p.read_bytes())
        pins = data.get("pins") or {}
        out: dict[str, dict] = {}
        for nid, rec in pins.items():
            if not isinstance(rec, dict):
                continue
            out[str(nid)] = {
                "pinned": bool(rec.get("pinned", False)),
                "x": rec.get("x"),
                "y": rec.get("y"),
            }
        return out
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Symbolic Plan artifacts (backward-compatible helpers)
# ---------------------------------------------------------------------------

def save_symbolic_plan(plan: Dict[str, Any], path: Path) -> None:
    """
    Persist a nodes-only symbolic plan to disk as JSON.

    Expected (tolerant) shape, historically:
      {
        "nodes": { "<id>": { "lane": int, "column": int, ... }, ... },
        "locks": {"hard": {...}, "buffers": {...}},
        "log": [ ... ],
        "meta": {...}
      }
    """
    try:
        _atomic_write_json(plan, Path(path))
    except Exception as e:
        raise LayoutIOError(f"Failed to save plan to {path}: {e}") from e


def load_symbolic_plan(path: Path) -> Dict[str, Any]:
    """
    Load a nodes-only symbolic plan from disk.
    Raises LayoutIOError if the file is missing or unreadable.
    """
    p = Path(path)
    if not p.exists():
        raise LayoutIOError(f"Plan file does not exist: {path}")
    try:
        data = _loads_bytes_dict(p.read_bytes())
        if not data:
            raise ValueError("empty or non-dict JSON")
        return data
    except Exception as e:
        raise LayoutIOError(f"Failed to read plan from {path}: {e}") from e


# ---------------------------------------------------------------------------
# Zones-specific helpers (new; per-zone hygiene)
# ---------------------------------------------------------------------------

def zone_layout_path(*, base_dir: Path | str, zone_id: str) -> Path:
    """
    Compute '<artifacts>/zones/<zone_id>/layout.json'.
    """
    artifacts = _artifacts_dir_from_base(base_dir)
    return _zone_dir_for(artifacts, zone_id) / "layout.json"

def zone_plan_path(*, base_dir: Path | str, zone_id: str) -> Path:
    """
    Compute '<artifacts>/zones/<zone_id>/plan.json'.
    """
    artifacts = _artifacts_dir_from_base(base_dir)
    return _zone_dir_for(artifacts, zone_id) / "plan.json"

def save_zone_layout(
    positions: Mapping[str, Tuple[float, float]],
    zoom: float,
    *,
    base_dir: Path | str,
    zone_id: str,
) -> Path:
    """
    Save a zone-only positions map under the sandbox artifacts tree.
    Returns the path written.
    """
    p = zone_layout_path(base_dir=base_dir, zone_id=zone_id)
    save_layout_to_disk(positions, zoom, p)
    return p

def load_zone_layout(
    *,
    base_dir: Path | str,
    zone_id: str,
) -> Tuple[Dict[str, Tuple[float, float]], float]:
    """
    Load a zone-only layout. Returns ({id:(x,y)}, zoom). Missing file -> ({},1.0).
    """
    p = zone_layout_path(base_dir=base_dir, zone_id=zone_id)
    try:
        return load_layout_from_disk(p)
    except LayoutIOError:
        return {}, 1.0

def save_zone_plan(
    plan: Dict[str, Any],
    *,
    base_dir: Path | str,
    zone_id: str,
) -> Path:
    """
    Save a zone-only symbolic plan to '<artifacts>/zones/<zone_id>/plan.json'.
    Returns the path written.
    """
    p = zone_plan_path(base_dir=base_dir, zone_id=zone_id)
    save_symbolic_plan(plan, p)
    return p

def load_zone_plan(
    *,
    base_dir: Path | str,
    zone_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Load a zone-only symbolic plan. Returns dict or None if missing.
    """
    p = zone_plan_path(base_dir=base_dir, zone_id=zone_id)
    try:
        return load_symbolic_plan(p)
    except LayoutIOError:
        return None
