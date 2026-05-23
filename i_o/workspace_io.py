from __future__ import annotations

import json
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Tuple

try:
    import tomllib as _tomllib  # py311+
except Exception:               # py310 fallback: optional
    _tomllib = None
try:
    import orjson  # type: ignore
except Exception:
    orjson = None  # type: ignore


class WorkspaceIOError(Exception):
    """Raised when reading or writing workspace/settings files fails."""


# ──────────────────────────────────────────────────────────────────────────────
# JSON helpers (fast path with orjson)
# ──────────────────────────────────────────────────────────────────────────────

def _dumps_bytes(obj: Any, *, pretty: bool = True) -> bytes:
    """
    Serialize to JSON bytes.

    - orjson with INDENT_2 + SORT_KEYS + trailing newline when pretty
    - stdlib json fallback (ensures newline when pretty)
    """
    if orjson is not None:
        opt = 0
        if pretty:
            opt |= orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS | orjson.OPT_APPEND_NEWLINE
        return orjson.dumps(obj, option=opt)

    text = json.dumps(
        obj,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
        sort_keys=pretty,
    )
    if pretty and not text.endswith("\n"):
        text += "\n"
    return text.encode("utf-8")


def _loads_bytes(data: bytes) -> Any:
    if orjson is not None:
        return orjson.loads(data)
    return json.loads(data.decode("utf-8", errors="ignore"))


# ──────────────────────────────────────────────────────────────────────────────
# JSON / TOML I/O
# ──────────────────────────────────────────────────────────────────────────────

def read_json(path: Path) -> Dict[str, Any]:
    """
    Tolerant JSON reader for small config-like payloads.
    Returns {} on missing/empty files.
    """
    try:
        p = Path(path)
        if not p.exists():
            return {}
        obj = _loads_bytes(p.read_bytes())
        return obj or {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        raise WorkspaceIOError(f"Failed to read JSON from {path}: {e}") from e


def write_json(data: Dict[str, Any], path: Path, *, pretty: bool = True) -> None:
    """
    Write JSON into the active workspace sandbox.

    The path **must** resolve under <store_root>/.pviz/artifacts or
    WorkspaceIOError is raised. This guarantees we never write into the
    scanned repository (scan_root) by mistake.
    """
    try:
        path = ensure_sandbox_path(Path(path))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_dumps_bytes(data, pretty=pretty))
    except Exception as e:
        raise WorkspaceIOError(f"Failed to write JSON to {path}: {e}") from e


def read_toml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    if _tomllib is None:  # pragma: no cover
        raise WorkspaceIOError("TOML support not available (requires Python 3.11+).")
    try:
        with path.open("rb") as f:
            return _tomllib.load(f)
    except Exception as e:
        raise WorkspaceIOError(f"Failed to read TOML from {path}: {e}") from e


# ──────────────────────────────────────────────────────────────────────────────
# Sandbox base (program-dir by default)
# ──────────────────────────────────────────────────────────────────────────────

def _program_root() -> Path:
    """
    Resolve the repository/program root relative to this file.

    File layout assumption:
      <repo>/
        i_o/workspace_io.py  ← you are here

    So parents[1] is <repo>.
    """
    return Path(__file__).resolve().parents[1]


def _default_app_cache_base() -> Path:
    """
    Preferred base directory for all sandboxed writes (store_root):

      1) If PVIZ_STORE_ROOT is set → use it (expanded & resolved).
      2) Otherwise, use <program_root>/.pviz_store.

    We intentionally avoid OS user caches (AppData, ~/.cache) to keep artifacts
    *inside* the program directory unless explicitly overridden.
    """
    env = os.environ.get("PVIZ_STORE_ROOT", "").strip()
    if env:
        try:
            base = Path(env).expanduser().resolve()
            base.mkdir(parents=True, exist_ok=True)
            return base
        except Exception:
            # fall through to program-dir default if env path is invalid
            pass

    base = (_program_root() / ".pviz_store").resolve()
    base.mkdir(parents=True, exist_ok=True)
    return base


# ──────────────────────────────────────────────────────────────────────────────
# Workspace path modeling
# ──────────────────────────────────────────────────────────────────────────────

def get_paths_or_raise() -> Tuple[Path, Path, Path, Path]:
    """
    Convenience: return (scan_root, store_root, pviz_dir, artifacts_dir)
    for the **active** workspace.

    - scan_root  → external project directory (read-only; analyzer ids are
                   derived relative to this)
    - store_root → sandbox base; all analyzer/UI artifacts live under here
    """
    wp = get_active_workspace()
    sr = wp.scan_root.resolve()
    st = wp.store_root.resolve()
    pv = wp.pviz_dir.resolve()
    ar = wp.artifacts_dir.resolve()
    return sr, st, pv, ar


def _workspace_id_for(path: Path) -> str:
    """
    Stable ID based on absolute scan_root path.

    Keeps store_root segregated per workspace so multiple repos don't collide
    in the sandbox.
    """
    p = str(path.resolve()).encode("utf-8")
    return hashlib.sha1(p).hexdigest()[:16]


_active_workspace: "WorkspacePaths" | None = None  # type: ignore[name-defined]


def set_active_workspace(paths: "WorkspacePaths") -> "WorkspacePaths":
    """
    Register the active workspace and eagerly ensure <store_root>/.pviz/artifacts exists.
    """
    (paths.pviz_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    global _active_workspace
    _active_workspace = paths
    return paths


def get_active_workspace() -> "WorkspacePaths":
    if _active_workspace is None:
        raise WorkspaceIOError("Workspace not initialized. Call set_active_workspace(...) first.")
    return _active_workspace


@dataclass(frozen=True)
class WorkspacePaths:
    """
    Paths for a single workspace.

    scan_root  → the external project directory (READ-ONLY; analyzer/ids are
                 derived relative to this)
    store_root → the app's sandbox/cache for ALL WRITES:

                  <base>/workspaces/<wsid>/
                  └─ .pviz/
                     └─ artifacts/

    NodeIds/EdgeIds used in analyzer artifacts are **always** derived from
    the scan_root side; store_root is purely for cached outputs.
    """
    scan_root: Path
    store_root: Path

    @property
    def pviz_dir(self) -> Path:
        return self.store_root / ".pviz"

    @property
    def artifacts_dir(self) -> Path:
        return self.pviz_dir / "artifacts"


def make_workspace_paths(scan_root: Path, *, app_base: Path | None = None) -> WorkspacePaths:
    """
    Construct a sandbox (store_root) for the given scan_root, creating:

      <app_base>/workspaces/<wsid>/.pviz/artifacts

    - app_base defaults to _default_app_cache_base() (program-dir based).
    - All directories are created eagerly.
    - scan_root is treated as a read-only anchor; **no** writes go under it.
    """
    scan_root = Path(scan_root).resolve()
    base = (Path(app_base).resolve() if app_base is not None else _default_app_cache_base())
    wsid = _workspace_id_for(scan_root)

    store = base / "workspaces" / wsid
    (store / ".pviz" / "artifacts").mkdir(parents=True, exist_ok=True)

    return WorkspacePaths(scan_root=scan_root, store_root=store.resolve())


def _is_within(child: Path, parent: Path) -> bool:
    try:
        c = child.resolve()
        p = parent.resolve()
        return p == c or p in c.parents
    except Exception:
        return False


def ensure_sandbox_path(path: Path, *, wp: WorkspacePaths | None = None) -> Path:
    """
    Ensure 'path' is inside <store_root>/.pviz/artifacts for the active workspace.

    Also enforces the global safety rule:

      - The sandbox itself must **not** live under scan_root.

    Returns resolved path or raises WorkspaceIOError.
    """
    wp = wp or get_active_workspace()
    target = Path(path).resolve()
    sandbox = (wp.pviz_dir / "artifacts").resolve()
    if not _is_within(target, sandbox):
        raise WorkspaceIOError(
            f"Refusing to write outside sandbox.\n  path={target}\n  sandbox={sandbox}"
        )
    # Also defend against store_root accidentally under scan_root:
    if _is_within(sandbox, wp.scan_root):
        raise WorkspaceIOError(
            "Security guard: sandbox lives under scan_root; "
            "choose a store_root outside the scanned repo."
        )
    return target


def placement_dir_for(seed: str, *, wp: WorkspacePaths | None = None) -> Path:
    """
    Convenience helper for scene/layout code:

      placement_<seed>/ directories always live under the sandbox's artifacts dir.

    This keeps layout/plan artifacts aligned with analyzer artifacts and under
    the same write guardrail.
    """
    wp = wp or get_active_workspace()
    d = (wp.pviz_dir / "artifacts" / f"placement_{seed}")
    d.mkdir(parents=True, exist_ok=True)
    return d.resolve()
