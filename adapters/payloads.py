# adapters/payloads.py
from __future__ import annotations
from typing import Any, Dict, Optional

from adapters.canonical import canon_module_id, to_posix


def _as_id(obj: Any) -> Optional[str]:
    """
    Try common id attributes in order of likelihood.
    Returns a string or None (no normalization here; callers normalize as needed).
    """
    for attr in ("node_id", "file_id", "uid", "uuid"):
        v = getattr(obj, attr, None)
        if v is not None:
            return str(v)
    return None


def _looks_like_pkg_init_router_ids(
    src_raw: str | None,
    dst_raw: str | None,
) -> bool:
    """
    Heuristic: detect intra-package __init__.py ↔ submodule edges using
    file-style ids (repo-relative POSIX or similar).

    Examples (true):
      mini_graph/__init__.py -> mini_graph/a.py
      mini_graph/__init__.py -> mini_graph/b/c.py

    Non-examples (false):
      __init__.py -> something.py          # ambiguous top-level
      a.py -> b.py                         # no __init__ involved
      pkg/__init__.py -> otherpkg/a.py     # crosses package boundary
    """
    def _norm(s: str | None) -> str:
        return (s or "").replace("\\", "/")

    s = _norm(src_raw)
    d = _norm(dst_raw)

    def _is_init(fid: str) -> bool:
        return fid.endswith("/__init__.py") or fid == "__init__.py"

    # One side must be __init__
    if not (_is_init(s) or _is_init(d)):
        return False

    init = s if _is_init(s) else d
    other = d if init is s else s

    # Strip package init suffix; bare "__init__.py" is too ambiguous
    if init.endswith("/__init__.py"):
        pkg = init[: -len("/__init__.py")]
    else:
        return False

    if not pkg:
        return False

    # Other must live inside same package and be a .py file
    return other.endswith(".py") and other.startswith(pkg + "/")


def edge_payload(edge_like: Any, *, scan_root: str | None = None) -> Dict[str, Any]:
    """
    Contracts-aligned payload builder for edges.

    Accepts:
      • EdgeItem-like (has .model_edge, .src_item, .dst_item), or
      • Already-normalized edge dict (JSON-safe).

    Returns a JSON-safe dict with at least: edge_id/src/dst and optional
    reasons/evidence/edge_index/meta/provenance.

    Note: src/dst are normalized to **dotted module ids** using
    canon_module_id(..., scan_root).
    """
    # Case 1: already a dict → shallow copy; assume upstream normalization,
    # including any 'provenance' key if present.
    if isinstance(edge_like, dict):
        return dict(edge_like)

    # Underlying model edge (if any)
    me = getattr(edge_like, "model_edge", None)

    # Endpoints → derive raw ids (typically repo-relative file ids) first
    src_item = getattr(edge_like, "src_item", None)
    dst_item = getattr(edge_like, "dst_item", None)
    src_id_raw = _as_id(src_item)
    dst_id_raw = _as_id(dst_item)

    # Heuristic: does this *look like* a pkg __init__ ↔ submodule edge?
    is_pkg_router = _looks_like_pkg_init_router_ids(src_id_raw, dst_id_raw)

    # Normalize to module-id form (root-aware when provided)
    try:
        src_id = canon_module_id(src_id_raw, scan_root) if src_id_raw else None
    except Exception:
        src_id = src_id_raw if src_id_raw is None else str(src_id_raw)

    try:
        dst_id = canon_module_id(dst_id_raw, scan_root) if dst_id_raw else None
    except Exception:
        dst_id = dst_id_raw if dst_id_raw is None else str(dst_id_raw)

    # Edge id (best-effort)
    eid_raw = _as_id(edge_like) or _as_id(me)
    eid = str(eid_raw) if eid_raw is not None else (
        f"{src_id}->{dst_id}" if (src_id and dst_id) else None
    )

    # Optional normalized fields (prefer those if present)
    reasons = getattr(me, "reasons", None)
    evidence = getattr(me, "evidence", None)
    edge_index = getattr(me, "edge_index", None)
    meta = getattr(me, "meta", None)
    provenance: Optional[str] = None

    # 1) If the model edge directly exposes provenance, prefer that.
    if isinstance(me, dict):
        prov = me.get("provenance")
        if isinstance(prov, str) and prov:
            provenance = prov
    else:
        prov = getattr(me, "provenance", None)
        if isinstance(prov, str) and prov:
            provenance = prov

    # 2) If meta has provenance, use that as a fallback.
    if provenance is None and isinstance(meta, dict):
        prov = meta.get("provenance")
        if isinstance(prov, str) and prov:
            provenance = prov

    # 3) If still unset, derive from the EdgeItem flag if present.
    if provenance is None:
        try:
            if getattr(edge_like, "is_synthesized_intra_pkg", False):
                provenance = "synth_pkg_router"
        except Exception:
            pass  # best-effort only

    # 4) If we *still* don't have provenance but the ids clearly look
    #    like pkg-init routing, classify them as such.
    if provenance is None and is_pkg_router:
        provenance = "synth_pkg_router"

    # Normalize / extend meta to include a boolean tag
    if meta is not None and not isinstance(meta, dict):
        # Best-effort coercion: try dict(meta), else wrap
        try:
            meta = dict(meta)  # type: ignore[arg-type]
        except Exception:
            meta = {"value": meta}
    if meta is None:
        meta = {}
    if is_pkg_router:
        # Tag for downstream consumers (overlays, panels, filters, etc.)
        meta.setdefault("synth_pkg_router", True)

    out: Dict[str, Any] = {"edge_id": eid, "src": src_id, "dst": dst_id}
    if reasons is not None:
        out["reasons"] = reasons
    if evidence is not None:
        out["evidence"] = evidence
    if edge_index is not None:
        out["edge_index"] = edge_index
    if meta:
        out["meta"] = meta
    if provenance is not None:
        out["provenance"] = provenance

    # Ensure JSON-safe strings where applicable
    if out["src"] is not None:
        out["src"] = str(out["src"])
    if out["dst"] is not None:
        out["dst"] = str(out["dst"])
    if out["edge_id"] is not None:
        out["edge_id"] = str(out["edge_id"])

    return out


def node_payload(node_like: Any, *, scan_root: str | None = None) -> Dict[str, Any]:
    """
    Contracts-aligned payload builder for nodes.

    Accepts:
      • NodeItem-like (has id-like attr; label/path/meta optional), or
      • Already-normalized node dict (JSON-safe).

    Returns a JSON-safe dict with at least: node_id; optional label/path/meta/kind.

    Note: node_id is normalized to **dotted module id** using canon_module_id(..., scan_root).
    """
    if isinstance(node_like, dict):
        # Normalize id + path/file if present, keep other fields as-is.
        out = dict(node_like)
        nid_guess = (
            out.get("node_id")
            or out.get("module")
            or out.get("name")
            or out.get("file")
            or out.get("path")
            or ""
        )
        try:
            out["node_id"] = canon_module_id(nid_guess, scan_root)
        except Exception:
            out["node_id"] = str(nid_guess or "")
        if "path" in out and out["path"] is not None:
            out["path"] = to_posix(out["path"])
        if "file" in out and out["file"] is not None:
            out["file"] = to_posix(out["file"])
        return out

    # Object-like: derive, then normalize
    nid_raw = (
        _as_id(node_like)
        or getattr(node_like, "module", None)
        or getattr(node_like, "name", None)
        or getattr(node_like, "file", None)
        or getattr(node_like, "path", None)
        or ""
    )
    try:
        nid = canon_module_id(nid_raw, scan_root)
    except Exception:
        nid = str(nid_raw or "")

    label = (
        getattr(node_like, "label", None)
        or getattr(node_like, "display", None)
        or getattr(node_like, "name", None)
    )
    path_raw = getattr(node_like, "file", None) or getattr(node_like, "path", None)
    path = to_posix(path_raw) if path_raw else None
    kind = getattr(node_like, "kind", None)
    meta = getattr(node_like, "meta", None)

    out: Dict[str, Any] = {"node_id": nid}
    if label is not None:
        out["label"] = str(label)
    if path is not None:
        out["path"] = str(path)
    if kind is not None:
        out["kind"] = str(kind)
    if meta is not None:
        out["meta"] = meta

    return out
