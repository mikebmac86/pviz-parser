from __future__ import annotations
import json
import os
import tempfile
from dataclasses import is_dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Tuple, TypedDict, Protocol, Optional

# --- Public exception ---------------------------------------------------------


class GraphJSONError(Exception):
    """Raised when graph JSON serialization or I/O fails."""


# --- Typed JSON schema (minimal, stable) -------------------------------------


class NodeDict(TypedDict, total=False):
    module: str
    path: str
    classes: list[dict]
    functions: list[dict]
    globals: list[dict]
    warnings: list[str]


class EdgeReasonDict(TypedDict, total=False):
    type: str
    detail: str


class EdgeDict(TypedDict, total=False):
    src: str
    dst: str
    reasons: list[EdgeReasonDict]


class LayoutDict(TypedDict):
    positions: Dict[str, Tuple[float, float]]
    zoom: float


class GraphJson(TypedDict, total=False):
    version: str
    nodes: Dict[str, NodeDict]
    edges: list[EdgeDict]
    layout: LayoutDict
    # Lightweight provenance; optional so older readers can ignore it.
    meta: Dict[str, Any]


# --- Duck-typed graph protocol (no hard import on core/graph) ----------------


class GraphLike(Protocol):
    nodes: Mapping[str, Any]
    edges: Iterable[Any]
    layout: Any


# --- Internal helpers ---------------------------------------------------------


def _atomic_write_json(payload: Mapping[str, Any], path: Path) -> None:
    """
    Atomic, durable-ish JSON write:
      - pretty, sorted keys
      - write to a temp file (prefer same directory), fsync, then replace
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)

    tmp_path: Optional[str] = None
    fd: Optional[int] = None
    try:
        # Prefer a temp file in the target directory for best atomicity.
        try:
            fd, tmp_path = tempfile.mkstemp(
                prefix=path.name + ".",
                dir=str(path.parent),
            )
        except Exception:
            # Fallback: use system temp directory if the target dir is not writable.
            fd, tmp_path = tempfile.mkstemp(
                prefix=path.name + ".",
                dir=None,  # default temp dir
            )

        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            fd = None  # fd is now owned by the file object
            f.write(data)
            try:
                f.flush()
                os.fsync(f.fileno())
            except Exception:
                pass

        # Move into place atomically.
        os.replace(tmp_path, path)

        # Best-effort directory fsync on the destination directory.
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            pass

    finally:
        # If something went wrong before os.replace, clean up the temp file.
        if tmp_path is not None:
            try:
                if os.path.exists(tmp_path) and os.path.abspath(tmp_path) != os.path.abspath(path):
                    os.remove(tmp_path)
            except Exception:
                pass


def _is_subpath(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def _resolve_repo_root(repo_root: Optional[Path]) -> Optional[Path]:
    """
    Normalize a provided repo/workspace root:
      - accept file or directory; if file, use its parent
      - resolve() to absolute
      - tolerate absence of analyzer.ensure_dir_root
    """
    if repo_root is None:
        return None
    try:
        # Prefer analyzer.ensure_dir_root if available
        from analyzer import ensure_dir_root  # type: ignore
        p = ensure_dir_root(Path(repo_root))
    except Exception:
        p = Path(repo_root)
    if p.is_file():
        p = p.parent
    return p.resolve()


def _guard_safe_destination(out_path: Path, repo_root: Optional[Path], allow_repo_writes: Optional[bool]) -> None:
    """
    Enforce scan/store separation:
      - If repo_root is provided and out_path is inside repo_root but *not* under .pviz/,
        require explicit opt-in (kwarg or PVIZ_ALLOW_REPO_WRITES=1).
    """
    if repo_root is None:
        return

    out_path = Path(out_path).resolve()
    repo_root = Path(repo_root).resolve()

    if not _is_subpath(out_path, repo_root):
        return  # not inside the scan root => OK

    # Allow writes anywhere under .pviz/ (including .pviz/artifacts)
    pviz_ok = any(part == ".pviz" for part in out_path.parts)
    if pviz_ok:
        return

    if allow_repo_writes is None:
        allow_repo_writes = os.environ.get("PVIZ_ALLOW_REPO_WRITES", "") == "1"

    if not allow_repo_writes:
        raise GraphJSONError(
            f"Refusing to write inside scan root:\n  {out_path}\n"
            f"Pass a destination under '<repo>/.pviz/' or set allow_repo_writes=True "
            f"(or PVIZ_ALLOW_REPO_WRITES=1) if you really intend to write into the repo."
        )


# --- Plain-object conversion helpers -----------------------------------------

def _to_plain(obj: Any) -> dict:
    """
    Best-effort conversion of various node/edge/reason objects into plain dicts.

    Accepts:
      - dict: returned as-is
      - objects with .to_dict()
      - dataclasses (via asdict)
      - objects with __dict__
      - simple primitives (str/int/float/bool/None) → wrapped as {"value": ...}

    The primitive wrapping avoids hard failures when analyzer payloads use
    simple values (e.g. reasons as strings) instead of full dicts.
    """
    if isinstance(obj, dict):
        return obj

    # Custom to_dict() hook
    if hasattr(obj, "to_dict") and callable(getattr(obj, "to_dict")):  # type: ignore[attr-defined]
        return obj.to_dict()  # type: ignore[return-value]

    # Dataclasses
    if is_dataclass(obj):
        return asdict(obj)

    # Plain Python objects
    if hasattr(obj, "__dict__"):
        return dict(obj.__dict__)

    # Primitive values – keep them, but wrap in a minimal dict
    if isinstance(obj, (str, int, float, bool, type(None))):
        return {"value": obj}

    # Anything else is genuinely unexpected
    raise GraphJSONError(f"Cannot convert object of type {type(obj).__name__} to dict")

def _coerce_position(value: Any) -> Tuple[float, float]:
    try:
        x, y = value
        return float(x), float(y)
    except Exception as e:  # pragma: no cover
        raise GraphJSONError(f"Invalid position format: {value!r}") from e


# --- Public API ---------------------------------------------------------------


def serialize_graph_to_json_dict(graph: GraphLike | Mapping[str, Any]) -> GraphJson:
    # Common metadata: provenance.
    meta: Dict[str, Any] = {
        "tool": "pviz-parser",
        "build": "0.1.5",
        "license": "MIT",
    }

    # Mapping-like path
    if isinstance(graph, Mapping) and "nodes" in graph and "edges" in graph:
        try:
            nodes_in = graph.get("nodes", {})
            edges_in = graph.get("edges", [])
            layout_in = graph.get("layout", {"positions": {}, "zoom": 1.0})

            nodes: Dict[str, NodeDict] = {}
            for nid, n in nodes_in.items():
                n_plain = _to_plain(n)
                nodes[str(nid)] = NodeDict(
                    module=str(n_plain.get("module", "")),
                    path=str(n_plain.get("path", "")),
                    classes=[_to_plain(c) for c in n_plain.get("classes", [])],
                    functions=[_to_plain(f) for f in n_plain.get("functions", [])],
                    globals=[_to_plain(g) for g in n_plain.get("globals", [])],
                    warnings=list(n_plain.get("warnings", []) or []),
                )

            edges: list[EdgeDict] = []
            for e in edges_in:
                e_plain = _to_plain(e)
                edges.append(
                    EdgeDict(
                        src=str(e_plain.get("src", "")),
                        dst=str(e_plain.get("dst", "")),
                        reasons=[_to_plain(r) for r in (e_plain.get("reasons") or [])],
                    )
                )

            layout_plain = _to_plain(layout_in)
            raw_pos = layout_plain.get("positions", {}) or {}
            positions = {str(k): _coerce_position(v) for k, v in raw_pos.items()}
            zoom = float(layout_plain.get("zoom", 1.0))

            return {
                "version": str(graph.get("version", "graph.v1")),
                "nodes": nodes,
                "edges": edges,
                "layout": {"positions": positions, "zoom": zoom},
                "meta": meta,
            }
        except Exception as e:
            raise GraphJSONError(f"Invalid graph mapping: {e}") from e

    # GraphLike path
    try:
        nodes: Dict[str, NodeDict] = {}
        for nid, n in graph.nodes.items():  # type: ignore[attr-defined]
            n_plain = _to_plain(n)
            nodes[str(nid)] = NodeDict(
                module=str(n_plain.get("module", "")),
                path=str(n_plain.get("path", "")),
                classes=[_to_plain(c) for c in n_plain.get("classes", [])],
                functions=[_to_plain(f) for f in n_plain.get("functions", [])],
                globals=[_to_plain(g) for g in n_plain.get("globals", [])],
                warnings=list(n_plain.get("warnings", []) or []),
            )

        edges: list[EdgeDict] = []
        for e in graph.edges:  # type: ignore[attr-defined]
            e_plain = _to_plain(e)
            edges.append(
                EdgeDict(
                    src=str(e_plain.get("src", "")),
                    dst=str(e_plain.get("dst", "")),
                    reasons=[_to_plain(r) for r in (e_plain.get("reasons") or [])],
                )
            )

        layout_plain = _to_plain(graph.layout)  # type: ignore[attr-defined]
        raw_pos = layout_plain.get("positions", {}) or {}
        positions = {str(k): _coerce_position(v) for k, v in raw_pos.items()}
        zoom = float(layout_plain.get("zoom", 1.0))

        return {
            "version": "graph.v1",
            "nodes": nodes,
            "edges": edges,
            "layout": {"positions": positions, "zoom": zoom},
            "meta": meta,
        }
    except Exception as e:
        raise GraphJSONError(f"Failed to serialize graph: {e}") from e


def export_graph_json(
    graph: GraphLike | Mapping[str, Any],
    out_path: Path,
    *,
    repo_root: Optional[Path] = None,
    allow_repo_writes: Optional[bool] = None,
) -> None:
    """
    Serialize a graph and write it to disk as JSON (pretty-printed), enforcing scan/store separation.

    Policy
    ------
    If repo_root is provided and `out_path` is inside that root but **not** under `.pviz/`,
    the write is refused unless `allow_repo_writes=True` (or env PVIZ_ALLOW_REPO_WRITES=1).

    This nudges callers to write under a store/sandbox path such as:
      <repo_root>/.pviz/artifacts/exports/graph.json

    Metadata
    --------
    The JSON payload includes a lightweight `meta` section with tool/build/license
    hints (tool name, version, license) so downstream consumers retain provenance.
    """
    data = serialize_graph_to_json_dict(graph)
    rr = _resolve_repo_root(repo_root)
    _guard_safe_destination(Path(out_path), rr, allow_repo_writes)
    try:
        _atomic_write_json(data, Path(out_path))
    except Exception as e:  # pragma: no cover
        raise GraphJSONError(f"Failed to write graph JSON to {out_path}: {e}") from e


def load_graph_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        raise GraphJSONError(f"Failed to read graph JSON from {path}: {e}") from e
