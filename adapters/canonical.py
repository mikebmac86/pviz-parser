#adapters/canonical.py
from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Optional, Union

Posixish = Union[str, Path, PurePosixPath]
Pathish = Union[str, Path]

# ─────────────────────────────────────────────────────────────────────────────
# Core utilities
# ─────────────────────────────────────────────────────────────────────────────

def to_posix(s: Posixish | None) -> str:
    """
    Normalize any path-like to POSIX separators without changing case or drive semantics.
    Idempotent for already-POSIX strings.

    NOTE:
      This DOES NOT make paths relative; it only normalizes separators.
    """
    s = "" if s is None else str(s)
    # PurePosixPath keeps case and avoids drive-letter munging on Windows
    return str(PurePosixPath(s.replace("\\", "/")))


def is_module_id(s: str | None) -> bool:
    """
    Heuristic for dotted module ids (for DISPLAY / labels):
      - no slashes,
      - no backslashes,
      - no '.py' suffix.

    Examples (True):
      'pkg', 'pkg.mod', 'scrapy.core.engine'

    Examples (False):
      'a/b/c.py', 'C:\\proj\\pkg\\mod.py', 'pkg.mod.py'
    """
    if not s:
        return False
    s = str(s)
    return ("/" not in s and "\\" not in s and not s.endswith(".py"))


def _normalize_root_arg(repo_root: Optional[Union[str, PurePosixPath, Path]]) -> Optional[str]:
    """
    Accept str/Path/PurePosixPath; if it's a file path, use its parent.
    Return POSIX string or None.

    IMPORTANT:
      This is for the *scan root* only (read-only). It defines the origin for
      repo-relative ids such as file-ids / NodeIds.
    """
    if not repo_root:
        return None
    try:
        p = Path(str(repo_root)).resolve()
        if p.is_file():
            p = p.parent
        return to_posix(p)
    except Exception:
        # Fallback: best-effort POSIX normalization
        return to_posix(str(repo_root))


# ─────────────────────────────────────────────────────────────────────────────
# SSOT Canonicalizers
#   • File ID (internal canonical / NodeId): repo-relative POSIX path
#       with '.py' or '/__init__.py'
#   • Module ID (display): dotted form (e.g. 'scrapy.core.engine')
# ─────────────────────────────────────────────────────────────────────────────

def pathish_for_module(mod: str, *, prefer_pkg_init: bool = False) -> str:
    """
    'a.b.c' -> 'a/b/c.py' (default) or 'a/b/c/__init__.py' if prefer_pkg_init=True.
    Does not consult the filesystem (pure transform).

    This is used as an intermediate step for turning dotted module ids into
    canonical file ids when needed.
    """
    m = (mod or "").strip().strip(".")
    if not m:
        return ""
    p = m.replace(".", "/")
    return f"{p}/__init__.py" if prefer_pkg_init else f"{p}.py"


def moduleish_for_path(pathish: str | Path) -> str:
    """
    'a/b/c.py'        -> 'a.b.c'
    'a/b/__init__.py' -> 'a.b'
    Dotted input is returned unchanged.

    This is DISPLAY-ONLY: do not use as a NodeId / file-id.
    """
    p = to_posix(pathish).rstrip("/")
    if not p:
        return ""
    if is_module_id(p):
        return p  # already dotted
    if p.endswith("/__init__.py"):
        p = p[: -len("/__init__.py")]
    elif p.endswith(".py"):
        p = p[:-3]
    return p.replace("/", ".")


def canon_path_rel(s: Posixish | None, scan_root: Posixish | None) -> str:
    """
    POSIX-normalize and make relative to the project's **scan root** if possible.

    This is the core helper for anchoring ids at the scan root:
      abs 'C:/.../scrapy-master/scrapy/core/engine.py'
        + scan_root 'C:/.../scrapy-master'
      → 'scrapy/core/engine.py'
    """
    p = to_posix(s)
    root = _normalize_root_arg(scan_root)
    if not root:
        return p
    try:
        root = root.rstrip("/")
        if not root:
            return p
        if p == root:
            rel = ""
        elif p.startswith(root + "/"):
            rel = p[len(root) + 1 :]
        else:
            # Outside of scan_root: leave as-is (absolute or arbitrary)
            rel = p
        return rel or p
    except Exception:
        return p


def canon_file_id(
    s: Posixish | None,
    scan_root: Posixish | None = None,
    *,
    prefer_pkg_init: bool = False,
    strict_suffix: bool = False,
) -> str:
    """
    Canonical **file id** for the visualizer (internal key; used for NodeId):

      • Accepts dotted module ids *or* path-ish strings (abs/rel).
      • Returns a repo-relative POSIX path with '.py' or '/__init__.py'
        WHEN the input is under scan_root or already relative.
      • Otherwise returns a POSIX-absolute or as-given path with normalized suffix.

    Options:
      prefer_pkg_init ->
        If input is dotted, prefer '/__init__.py' over '.py'.

      strict_suffix   ->
        If True and input looks like a path without '.py' or '/__init__.py',
        do NOT append '.py'.
        If False (default), append '.py' when the input looks clearly path-like.

    This is the SSOT for **NodeId**:
      NodeId ≡ canon_file_id(path_or_module, scan_root)
    """
    if not s:
        return ""
    raw = str(s).strip()

    # If dotted, synthesize path-ish first
    if is_module_id(raw):
        raw = pathish_for_module(raw, prefer_pkg_init=prefer_pkg_init)

    # POSIX + repo-relative if possible
    p = canon_path_rel(raw, scan_root)

    # Normalize suffix
    if p.endswith("/__init__.py") or p.endswith(".py"):
        return p

    looks_like_path = ("/" in p) or ("\\" in p)
    if not strict_suffix and looks_like_path:
        return p.rstrip("/") + ".py"

    return p


def canon_module_id(
    s: Posixish | None,
    scan_root: Posixish | None = None,
) -> str:
    """
    Canonical **module id** (dotted) for display/labels.

      • If path-like: normalize to POSIX, make repo-relative if possible,
        then convert to dotted form.
      • If dotted: returned as-is.

    Examples:
      path='scrapy/core/engine.py'           → 'scrapy.core.engine'
      path='C:/proj/scrapy/core/engine.py',
        scan_root='C:/proj'                 → 'scrapy.core.engine'
      dotted='scrapy.core.engine'           → 'scrapy.core.engine'
    """
    if not s:
        return ""
    raw = str(s).strip()
    if is_module_id(raw):
        return raw
    return moduleish_for_path(canon_path_rel(raw, scan_root))


def canon_node_id(
    s: Posixish | None,
    scan_root: Posixish | None = None,
    *,
    prefer_pkg_init: bool = False,
    strict_suffix: bool = False,
) -> str:
    """
    Convenience alias for the NodeId contract:

      NodeId == canonical file-id anchored at scan_root.

    This is functionally identical to canon_file_id, but calling sites that
    think explicitly in terms of 'NodeId' should prefer this helper for clarity.
    """
    return canon_file_id(
        s,
        scan_root,
        prefer_pkg_init=prefer_pkg_init,
        strict_suffix=strict_suffix,
    )


def repo_rel(path: Optional[Pathish], root: Optional[Pathish]) -> Optional[str]:
    """
    Return POSIX path relative to scan root when possible; otherwise absolute POSIX.

    None in -> None out.

    This is similar to canon_path_rel, but works with Path objects and
    uses Path.relative_to (raising on mismatch).
    """
    if path is None:
        return None
    p = Path(path)
    try:
        if root:
            r = Path(root).resolve()
            pp = p.resolve()
            rel = pp.relative_to(r)
            return to_posix(str(rel))
    except Exception:
        pass
    # Fallback: absolute or as-given, but POSIX
    try:
        pp = p.resolve()
        return to_posix(str(pp))
    except Exception:
        return to_posix(str(p))


def is_repo_relative(p: Optional[str]) -> bool:
    """
    A conservative check: treat as repo-relative only if:
      - it’s a non-empty string,
      - it has no drive/colon,
      - and it does not start with a root slash.

    This is used as a guard when deciding whether an id already looks
    anchored at the scan root.
    """
    if not isinstance(p, str) or not p:
        return False
    if ":" in p:
        return False
    return not to_posix(p).startswith("/")


# ─────────────────────────────────────────────────────────────────────────────
# Root normalization (Path) and scan-root discovery
# ─────────────────────────────────────────────────────────────────────────────

def normalize_root(repo_root: Optional[Pathish]) -> Optional[Path]:
    """
    Resolve to an absolute Path (file -> parent dir). Returns None if falsy/invalid.

    This is used primarily by workspace / scan-root selection.
    """
    if not repo_root:
        return None
    try:
        p = Path(str(repo_root)).expanduser().resolve(strict=False)
        return p.parent if p.is_file() else p
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# ID variants (module/path → canonical variants)
# ─────────────────────────────────────────────────────────────────────────────

def id_variants(token: str, scan_root: Optional[Pathish] = None) -> list[str]:
    """
    Produce plausible, de-duplicated variants for a token:

      • original (POSIX-normalized)
      • file-id variants: a/b/c.py and a/b/c/__init__.py (strict_suffix=False)
      • module-id variant: a.b.c (canon_module_id)

    Order is preserved; all outputs are POSIX normalized.

    This is mainly for fuzzier matching (e.g., resolving user input or legacy
    ids back to a canonical NodeId / file-id or module id).
    """
    out: list[str] = []
    seen: set[str] = set()

    def _emit(v: Optional[str]) -> None:
        if not v:
            return
        s = to_posix(v)
        if s and s not in seen:
            seen.add(s)
            out.append(s)

    _emit(token)
    try:
        _emit(canon_file_id(token, scan_root, prefer_pkg_init=False, strict_suffix=False))
    except Exception:
        pass
    try:
        _emit(canon_file_id(token, scan_root, prefer_pkg_init=True, strict_suffix=False))
    except Exception:
        pass
    try:
        _emit(canon_module_id(token, scan_root))
    except Exception:
        pass
    return out
