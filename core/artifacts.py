from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import tempfile
import time
import random

from diagnostics.logging import log_event  # ← central logger

try:
    import orjson  # type: ignore
except Exception:
    orjson = None  # type: ignore
import json  # fallback


# ---------------------------------------------------------------------------
# Logging shim → central diagnostics
# ---------------------------------------------------------------------------
def _log(kind: str, *parts, **fields) -> None:
    """
    Route artifact diagnostics into the shared logger.

    Local callers pass a short kind like:
      - "WRITE"
      - "READ"
      - "READ_FAIL"
      - "PRUNE"

    They are exposed as:
      - "ART:WRITE"
      - "ART:READ"
      - "ART:READ_FAIL"
      - "ART:PRUNE"
    """
    try:
        log_event(f"ART:{kind}", *parts, **fields)
    except Exception:
        # Never let diagnostics break artifact I/O.
        pass


@dataclass
class ArtCtx:
    root: Path           # .../.pviz/artifacts
    mode: str            # "classic" | "zones"
    run_id: str          # "classic" or zones_<timestamp>


def _normalize_artifacts_root(base: Path) -> Path:
    """
    Accepts:
      • <store_root>
      • <store_root>/.pviz
      • <store_root>/.pviz/artifacts
    Returns: <store_root>/.pviz/artifacts
    """
    base = Path(base).resolve()
    if base.name == "artifacts" and base.parent.name == ".pviz":
        return base
    if base.name == ".pviz":
        return (base / "artifacts").resolve()
    # assume it's a store_root
    return (base / ".pviz" / "artifacts").resolve()


def make_ctx(store_root: Path, *, mode: str) -> ArtCtx:
    """
    Create an artifact context rooted under the sandbox store_root:

        store_root/.pviz/artifacts
    """
    if store_root is None:
        raise RuntimeError("make_ctx: store_root is None (workspace/store root must be set)")
    root = _normalize_artifacts_root(Path(store_root))
    root.mkdir(parents=True, exist_ok=True)

    if mode == "classic":
        rid = "classic"
    else:
        # add microseconds and a short random suffix to avoid collisions
        _us = int(time.time() * 1_000_000) % 1_000_000
        _rs = f"{random.randrange(16**3):03x}"
        rid = f"{time.strftime('zones_%Y%m%d-%H%M%S')}_{_us:06d}_{_rs}"
    return ArtCtx(root=root, mode=mode, run_id=rid)


def _dir(ctx: ArtCtx, *parts: str) -> Path:
    p = ctx.root.joinpath(*parts)
    p.mkdir(parents=True, exist_ok=True)
    return p


def path_for(ctx: ArtCtx, role: str, *, seed: Optional[str] = None) -> Path:
    """
    role:
      - "classic/full_edges"         -> artifacts/classic/edges/full.json
      - "classic/layout"             -> artifacts/classic/layout/layout.json
      - "zones/plan"                 -> artifacts/<run_id>/plan/plan.json
      - "zones/placement_snapshot"   -> artifacts/<run_id>/placement_accum/layout.json
      - "zones/edges"                -> artifacts/<run_id>/edges/<seed>.json
    """
    if role == "classic/full_edges":
        return _dir(ctx, "classic", "edges") / "full.json"
    if role == "classic/layout":
        return _dir(ctx, "classic", "layout") / "layout.json"
    if role == "zones/plan":
        return _dir(ctx, ctx.run_id, "plan") / "plan.json"
    if role == "zones/placement_snapshot":
        return _dir(ctx, ctx.run_id, "placement_accum") / "layout.json"
    if role == "zones/edges":
        name = (seed or "accum") + ".json"
        return _dir(ctx, ctx.run_id, "edges") / name
    raise ValueError(f"unknown artifact role: {role}")


# ---------------- JSON helpers (bytes I/O; pretty by default) ----------------

def _dumps_bytes(obj, *, pretty: bool = True) -> bytes:
    if orjson is not None:
        opt = orjson.OPT_APPEND_NEWLINE
        if pretty:
            opt |= orjson.OPT_SORT_KEYS | orjson.OPT_INDENT_2
        return orjson.dumps(obj, option=opt)
    # stdlib fallback
    if pretty:
        s = json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False)
        if not s.endswith("\n"):
            s += "\n"
    else:
        s = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    return s.encode("utf-8")


def _loads_bytes(data: bytes):
    if orjson is not None:
        return orjson.loads(data)
    return json.loads(data.decode("utf-8", errors="ignore"))


# ------------------------ atomic read / write wrappers -----------------------

def write_json_atomic(path: Path, payload) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file in the same directory (bytes) then replace.
    with tempfile.NamedTemporaryFile("wb", delete=False, dir=str(path.parent), suffix=".tmp") as f:
        f.write(_dumps_bytes(payload, pretty=True))
        tmp = Path(f.name)
    tmp.replace(path)


def trace_write(window, path: Path, role: str, **tags):
    _log("WRITE", role, str(path), **tags)
    try:
        getattr(window, "_art_writes").append((role, str(path)))
    except Exception:
        try:
            window._art_writes = [(role, str(path))]
        except Exception:
            pass


def trace_read(window, path: Path, role: str, **tags):
    _log("READ", role, str(path), **tags)
    try:
        getattr(window, "_art_reads").append((role, str(path)))
    except Exception:
        try:
            window._art_reads = [(role, str(path))]
        except Exception:
            pass


def save_json(window, ctx: ArtCtx, role: str, payload, *, seed: Optional[str] = None) -> Path:
    p = path_for(ctx, role, seed=seed)
    write_json_atomic(p, payload)
    trace_write(window, p, role, seed=seed or "")
    return p


def load_json(window, ctx: ArtCtx, role: str, *, seed: Optional[str] = None, default=None):
    p = path_for(ctx, role, seed=seed)
    if not p.exists():
        return default
    try:
        data = _loads_bytes(p.read_bytes())
        trace_read(window, p, role, seed=seed or "")
        return data
    except Exception as e:
        _log("READ_FAIL", role, str(p), err=repr(e))
        return default


def prune_temp_files(root: Path) -> None:
    # remove legacy temp seed files e.g. zones@v1.json.*
    for pat in ("zones@v1.json.*",):
        for fp in Path(root).glob(pat):
            _log("PRUNE", str(fp))
            try:
                fp.unlink(missing_ok=True)
            except Exception:
                pass
