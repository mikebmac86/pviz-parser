from __future__ import annotations
from dataclasses import is_dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Optional
from .workspace_io import read_json, write_json, WorkspaceIOError
from analyzer import Workspace as _WS, merge_includes
from adapters.canonical import to_posix

# --- Runtime glue ----------------------------------------------------------------
# 1) Make analyzer.Workspace constructor tolerant to extra kwargs (includes/excludes/meta)
# 2) Make Workspace iterable over its files so graphkit.build_graph(ws) "just works".

if not hasattr(_WS, "_pviz_patched"):
    _orig_init = _WS.__init__  # type: ignore

    def _init_loose(self, *args, **kwargs):
        # Accept unknown kwargs like includes/excludes/meta without crashing.
        root = kwargs.pop("root", None)
        files = kwargs.pop("files", None)
        # Call original __init__ with 0/1/2 positional args as appropriate.
        if files is not None:
            try:
                _orig_init(self, root, files)  # type: ignore[misc]
            except TypeError:
                _orig_init(self, root)  # type: ignore[misc]
        elif root is not None:
            _orig_init(self, root)  # type: ignore[misc]
        else:
            _orig_init(self)  # type: ignore[misc]
        # Attach any extra, UI-facing fields so they round-trip in JSON.
        for k, v in kwargs.items():
            setattr(self, k, v)

    _WS.__init__ = _init_loose  # type: ignore[assignment]

    if not hasattr(_WS, "__iter__"):
        def __iter__(self):
            return iter(getattr(self, "files", []) or [])
        _WS.__iter__ = __iter__  # type: ignore[assignment]

    _WS._pviz_patched = True  # type: ignore[attr-defined]


def _to_posix_str(p: Any) -> Optional[str]:
    if p is None or p == "":
        return None
    try:
        return Path(p).expanduser().resolve().as_posix()
    except Exception:
        # Last-resort: string normalize slashes
        try:
            return to_posix(p)
        except Exception:
            return None


def _ws_to_dict(ws: _WS) -> Dict[str, Any]:
    """Serialize a Workspace to a mapping, preserving extra fields if present."""
    out: Dict[str, Any] = {}
    # Known fields — persist root as POSIX string for consistency across modules
    out["root"] = _to_posix_str(getattr(ws, "root", None))
    # Files are already normalized by merge_includes on load; still coerce to str list here
    out["files"] = [str(f) for f in (getattr(ws, "files", []) or [])]
    # Preserve optional UI fields if present
    for k in ("includes", "excludes", "meta"):
        v = getattr(ws, k, None)
        if v:
            out[k] = v if not is_dataclass(v) else asdict(v)
    return out


def _ws_from_dict(d: Dict[str, Any]) -> _WS:
    """Leniently hydrate a Workspace; tolerate older/newer shapes."""
    root_raw = d.get("root") or d.get("base")
    # Accept str/Path; keep original analyzer.Workspace contract (Path|str|None)
    root: Optional[str | Path] = _to_posix_str(root_raw)
    files = [str(x) for x in (d.get("files") or [])]
    ws = _WS(root=root, files=files)
    # Reattach optional fields for UI friendliness
    for k in ("includes", "excludes", "meta"):
        if k in d:
            setattr(ws, k, d[k])
    return ws


class WorkspaceManager:
    """
    Thin manager expected by the UI:

      • load(path)        -> Workspace
      • save_atomic(ws,path)
      • set_active(ws) / get_active()

    Also provides small conveniences for callers that want build_graph kwargs.
    """
    _active: Optional[_WS] = None

    @classmethod
    def load(cls, path: Path) -> _WS:
        data = read_json(Path(path))
        if not isinstance(data, dict):
            data = {}
        ws = _ws_from_dict(data)

        # Normalize file path strings and dedup (policy via merge_includes)
        try:
            ws.files = merge_includes([], ws.files)
        except Exception:
            pass
        return ws

    @classmethod
    def save_atomic(cls, ws: _WS, path: Path, *, pretty: bool = True) -> None:
        """
        Atomic-ish write using a temp sibling; falls back to direct write on error.
        """
        path = Path(path)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = _ws_to_dict(ws)

        try:
            write_json(payload, tmp, pretty=pretty)
            tmp.replace(path)
        except Exception:
            # Best-effort fallback; re-raise as WorkspaceIOError if it fails too.
            try:
                write_json(payload, path, pretty=pretty)
            except Exception as e:
                raise WorkspaceIOError(f"Failed to persist workspace: {e}") from e
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass

    @classmethod
    def set_active(cls, ws: _WS) -> None:
        cls._active = ws

    @classmethod
    def get_active(cls) -> Optional[_WS]:
        return cls._active

    # ---- Optional helpers ------------------------------------------------------

    @classmethod
    def ensure_file(cls, path: Path) -> _WS:
        """
        Ensure a workspace file exists at `path`; if missing, create a minimal one.
        """
        p = Path(path)
        if not p.exists():
            ws = _WS(root=None, files=[])
            # If UI added includes/excludes/meta to a fresh object, keep parity:
            setattr(ws, "includes", [])
            setattr(ws, "excludes", [])
            setattr(ws, "meta", {"created_by": "pviz"})
            cls.save_atomic(ws, p)
            return ws

        return cls.load(p)

    @classmethod
    def as_build_kwargs(cls, ws: _WS) -> Dict[str, Any]:
        """
        Convenience for graph builders that want explicit kwargs.
        Returns: {"files": Iterable[str], "root": Optional[Path]}
        """
        root = Path(getattr(ws, "root", "")).resolve() if getattr(ws, "root", None) else None
        return {"files": list(getattr(ws, "files", []) or []), "root": root}
