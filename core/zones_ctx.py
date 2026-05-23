# core/zones_ctx.py
#
# Headless zones context — no ui/ dependencies.
# All types previously imported from ui/modes/zones_downward/ are inlined here.
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from adapters.canonical import to_posix
from diagnostics.logging import log_event


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log(kind: str, **fields: Any) -> None:
    try:
        log_event(f"ZONES:{kind}", **fields)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Inlined types (previously from ui/modes/zones_downward/state.py)
# ---------------------------------------------------------------------------

@dataclass
class Point:
    x: float
    y: float


@dataclass
class Cell:
    lane: int
    column: int
    px_x: Optional[float] = None
    px_y: Optional[float] = None


@dataclass
class ZoneMeta:
    seed_id: str
    created_at: str
    label: Optional[str]
    origin: Cell
    seq: int
    footprint_lanes: int = 1
    footprint_columns: int = 1


@dataclass
class ZoneState:
    pos: Point
    size: Point
    z: int = 0
    resizable: bool = False


@dataclass
class ZoneLayout:
    node_ids: List[str] = field(default_factory=list)
    local_positions: Dict[str, Point] = field(default_factory=dict)
    member_files: List[str] = field(default_factory=list)


@dataclass
class Zone:
    zone_id: str
    meta: ZoneMeta
    state: ZoneState
    layout: ZoneLayout


class SeenSet:
    def __init__(self) -> None:
        self._seen: set[str] = set()

    def add_many(self, ids: List[str]) -> None:
        self._seen.update(ids)

    def add(self, node_id: str) -> None:
        self._seen.add(node_id)

    def has(self, node_id: str) -> bool:
        return node_id in self._seen

    def __contains__(self, node_id: str) -> bool:
        return node_id in self._seen

    def as_set(self) -> set[str]:
        return set(self._seen)

    def __len__(self) -> int:
        return len(self._seen)

    def __iter__(self):
        return iter(self._seen)

    def __bool__(self) -> bool:
        return bool(self._seen)

    def __repr__(self) -> str:
        return f"SeenSet(n={len(self)})"


# ---------------------------------------------------------------------------
# ModeContext (previously from ui/modes/zones_downward/ui_flow.py)
# ---------------------------------------------------------------------------

@dataclass
class ModeContext:
    zones: List[Zone]
    seen: SeenSet
    seq: int
    artifact_dir: str

    @property
    def store_dir(self) -> str:
        return self.artifact_dir


# ---------------------------------------------------------------------------
# Zone persistence (previously from ui/modes/zones_downward/persistence.py)
# ---------------------------------------------------------------------------

ZONES_VERSION = "zones@v1"


def _as_int(val, default: int = 0) -> int:
    try:
        return int(val)
    except Exception:
        try:
            return int(float(val))
        except Exception:
            return default


def _zone_from_dict(d: Dict) -> Zone:
    meta   = d.get("meta")   or {}
    state  = d.get("state")  or {}
    layout = d.get("layout") or {}
    foot   = meta.get("footprint") or {}
    o      = meta.get("origin")    or {}
    pos    = state.get("pos")  or {}
    size   = state.get("size") or {}

    return Zone(
        zone_id=d.get("zone_id", "zone"),
        meta=ZoneMeta(
            seed_id=meta.get("seed_id", ""),
            created_at=meta.get("created_at", ""),
            label=meta.get("label"),
            origin=Cell(
                lane=_as_int(o.get("lane", 0)),
                column=_as_int(o.get("column", 0)),
                px_x=o.get("px_x"),
                px_y=o.get("px_y"),
            ),
            seq=_as_int(meta.get("seq", 0)),
            footprint_lanes=_as_int(foot.get("lanes", 1), 1),
            footprint_columns=_as_int(foot.get("columns", 1), 1),
        ),
        state=ZoneState(
            pos=Point(_as_int(pos.get("x", 0)), _as_int(pos.get("y", 0))),
            size=Point(_as_int(size.get("w", 0)), _as_int(size.get("h", 0))),
            z=_as_int(state.get("z", 0)),
            resizable=bool(state.get("resizable", False)),
        ),
        layout=ZoneLayout(
            node_ids=list(layout.get("node_ids", [])),
            local_positions={
                k: Point(_as_int(p.get("x", 0)), _as_int(p.get("y", 0)))
                for k, p in (layout.get("local_positions") or {}).items()
                if isinstance(p, dict)
            },
            member_files=list(layout.get("member_files", [])),
        ),
    )


def _load_zones(path: Path) -> List[Zone]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as ex:
        _log("zones_load_corrupt", file=str(path), err=repr(ex))
        return []
    zones = []
    for rec in (data.get("zones") or []):
        try:
            zones.append(_zone_from_dict(rec or {}))
        except Exception as ex:
            _log("zones_load_entry_error", err=repr(ex))
    _log("zones_loaded", file=str(path), count=len(zones))
    return zones


# ---------------------------------------------------------------------------
# Path normalization (previously from ui/modes/zones_downward/controller/lifecycle.py)
# ---------------------------------------------------------------------------

def _normalize_artifacts_dir(p: Path) -> Path:
    return Path(p).resolve()


# ---------------------------------------------------------------------------
# Core context builder
# ---------------------------------------------------------------------------

def build_zones_context(artifacts_dir: Path) -> ModeContext:
    adir = _normalize_artifacts_dir(artifacts_dir)
    zfile = adir / "zones@v1.json"

    zones: List[Zone] = []
    seq = 0
    try:
        if zfile.exists():
            zones = _load_zones(zfile)
            if zones:
                seq = max(
                    getattr(getattr(z, "meta", None), "seq", 0)
                    for z in zones
                    if getattr(z, "meta", None) is not None
                )
    except Exception as e:
        _log("zones_ctx_load_err", file=str(zfile), err=repr(e))
        zones, seq = [], 0

    seen = SeenSet()
    for z in zones:
        try:
            node_ids = list(
                getattr(getattr(z, "layout", None), "node_ids", []) or []
            )
            seen.add_many(node_ids)
        except Exception as e:
            _log("zones_ctx_seen_err", zone=getattr(z, "zone_id", "?"), err=repr(e))

    ctx = ModeContext(
        zones=zones,
        seen=seen,
        seq=seq,
        artifact_dir=to_posix(str(adir)),
    )
    setattr(ctx, "active_dir", adir)

    _log(
        "zones_ctx_ready",
        store=str(adir),
        zones=len(zones),
        seq=seq,
        seen_len=len(seen),
    )
    return ctx


# ---------------------------------------------------------------------------
# Artifacts-dir resolver
# ---------------------------------------------------------------------------

def _is_under(child: Path, parent: Path) -> bool:
    try:
        return parent.resolve() in child.resolve().parents or child.resolve() == parent.resolve()
    except Exception:
        return False


def resolve_zones_artifacts_dir_core(
    *,
    store_root: Path,
    fallback: Path,
    prior_ctx: Optional[Any] = None,
    window_dir: Optional[Any] = None,
) -> Path:
    store_root = Path(store_root).resolve()

    if prior_ctx is not None:
        cand_raw = getattr(prior_ctx, "artifact_dir", None)
        if cand_raw:
            try:
                cand = Path(cand_raw)
                if cand.exists() and _is_under(cand, store_root):
                    _log("zones_ctx_reuse_prior", path=str(cand))
                    return cand.resolve()
            except Exception as e:
                _log("zones_ctx_reuse_prior_err", err=repr(e))

    if window_dir is not None:
        try:
            cand = Path(window_dir)
            if cand.exists() and _is_under(cand, store_root):
                _log("zones_ctx_reuse_window", path=str(cand))
                return cand.resolve()
        except Exception as e:
            _log("zones_ctx_reuse_window_err", err=repr(e))

    out = _normalize_artifacts_dir(fallback)
    _log("zones_ctx_fallback", path=str(out))
    return out

# ---------------------------------------------------------------------------
# Discovery graph path helper
# ---------------------------------------------------------------------------

def discovery_graph_path(artifacts_dir: Path) -> Path:
    return Path(artifacts_dir).resolve() / "discovery" / "graph.json"