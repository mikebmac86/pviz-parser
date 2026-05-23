# backend/saas_analyzer/analyzer/ts/canonical_web.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional, Union, Callable
import posixpath
import re

# Keep your existing aliases
Posixish = Union[str, Path, PurePosixPath]
Pathish = Union[str, Path]

# ─────────────────────────────────────────────────────────────────────────────
# Core primitives (safe across languages)
# ─────────────────────────────────────────────────────────────────────────────

_DRIVE_RE = re.compile(r"^[A-Za-z]:(/|\\|$)")  # "C:\", "C:/", "C:"
_UNC_RE = re.compile(r"^(\\\\|//)")           # "\\server\share" or "//server/share"


def to_posix(s: Posixish | None) -> str:
    """
    Convert any path-like to POSIX separators. Does NOT attempt to resolve symlinks
    or touch the filesystem.

    IMPORTANT: This *also* normalizes redundant separators, '.' and '..' segments,
    while preserving case.
    """
    if s is None:
        return ""
    raw = str(s)

    # Convert slashes first
    raw = raw.replace("\\", "/")

    # Preserve a leading UNC marker "//" if present; normpath would collapse it.
    is_unc = raw.startswith("//")

    # Normalize '.' and '..' segments and redundant slashes.
    # posixpath.normpath("") -> ".": we prefer "" for empties.
    normed = posixpath.normpath(raw)
    if normed == ".":
        normed = ""

    if is_unc and not normed.startswith("//"):
        normed = "//" + normed.lstrip("/")

    return normed


def _strip_leading_current_dir(p: str) -> str:
    """
    Remove leading "./" segments and a single leading "/" if present,
    but DO NOT destroy "../" parent traversal.
    """
    p = to_posix(p).strip()
    while p.startswith("./"):
        p = p[2:]
    if p.startswith("/"):
        p = p[1:]
    return p


def _looks_windows_drive(p: str) -> bool:
    return bool(_DRIVE_RE.match(p))


def _looks_unc(p: str) -> bool:
    return bool(_UNC_RE.match(p))


def is_path_absolute_like(p: str) -> bool:
    r"""
    True for:
      - POSIX absolute: "/x/y"
      - UNC: "//server/share"
      - Windows drive: "C:/.." or "C:\..." (after normalization -> "C:/....")
    """
    if not p:
        return False
    p2 = p.strip()
    if not p2:
        return False

    # normalize slashes for checks
    p2 = p2.replace("\\", "/")
    if p2.startswith("/"):
        return True
    if _looks_unc(p2):
        return True
    if _looks_windows_drive(p2):
        return True
    return False


def _normalize_root_arg(repo_root: Optional[Union[str, PurePosixPath, Path]]) -> Optional[str]:
    """
    Accept str/Path/PurePosixPath; if it's a file path, use its parent.
    Return normalized POSIX string or None.

    NOTE: We resolve() only for real Path inputs to get a stable absolute root.
    For string-like inputs that might be non-existent, we still normalize safely.
    """
    if not repo_root:
        return None
    try:
        p = Path(str(repo_root)).resolve()
        if p.is_file():
            p = p.parent
        return to_posix(p)
    except Exception:
        # Fall back to pure normalization; may remain relative.
        return to_posix(str(repo_root))


def canon_path_rel(s: Posixish | None, scan_root: Posixish | None) -> str:
    """
    POSIX-normalize and make relative to scan_root if possible.

    Rules:
      - If s is already repo-relative, normalize it and return.
      - If s is absolute (POSIX/UNC/drive) and is under scan_root, relativize.
      - Otherwise return normalized s unchanged.

    Never emits paths containing '/../' segments.
    """
    p = to_posix(s)
    if not p:
        return ""

    root = _normalize_root_arg(scan_root)
    if not root:
        # If no root, just ensure no leading "./"
        return _strip_leading_current_dir(p)

    root = to_posix(root).rstrip("/")
    if not root:
        return _strip_leading_current_dir(p)

    # If p is already repo-relative, just normalize and return.
    if not is_path_absolute_like(p):
        return _strip_leading_current_dir(p)

    # Absolute path: relativize only if it's under root (string-prefix safe on POSIX normalized)
    # Ensure root has leading "/" or "C:/" etc and compare with a trailing slash boundary.
    if p == root:
        return ""  # exact root path
    if p.startswith(root + "/"):
        rel = p[len(root) + 1 :]
        return _strip_leading_current_dir(to_posix(rel))
    return p  # not under root -> leave absolute-ish


def is_repo_relative(p: Optional[str]) -> bool:
    """
    Conservative check for repo-relative ids.

    Repo-relative means:
      - non-empty string
      - not a URL
      - not UNC / drive / leading '/'
    """
    if not isinstance(p, str) or not p.strip():
        return False

    s = to_posix(p).strip()
    if not s:
        return False

    # quick URL check
    low = s.lower()
    if low.startswith(("http://", "https://", "data:", "blob:")):
        return False

    if is_path_absolute_like(s):
        return False

    # also reject "C:" like tokens even if weirdly formatted
    if _looks_windows_drive(s):
        return False

    return True


def canon_join_rel(base_rel: str, spec_rel: str) -> str:
    """
    Join two repo-relative-ish POSIX paths and normalize '.'/'..' segments.
    """
    base_rel = _strip_leading_current_dir(to_posix(base_rel))
    spec_rel = _strip_leading_current_dir(to_posix(spec_rel))
    base_dir = posixpath.dirname(base_rel)
    joined = posixpath.normpath(posixpath.join(base_dir, spec_rel))
    if joined == ".":
        joined = ""
    return joined


# ─────────────────────────────────────────────────────────────────────────────
# JS/TS-specific canonicalization
# ─────────────────────────────────────────────────────────────────────────────

JS_EXTS = (".js", ".mjs", ".cjs", ".jsx")
TS_EXTS = (".ts", ".tsx")
WEB_EXTS = JS_EXTS + TS_EXTS

INDEX_BASENAMES = ("index",)  # can extend (e.g., "main") if you want


def has_web_ext(path: str) -> bool:
    lp = (path or "").lower()
    return any(lp.endswith(ext) for ext in WEB_EXTS)


def strip_query_and_hash(spec: str) -> str:
    """
    Handle weird cases: 'foo.js?x=1#y' → 'foo.js'
    """
    s = (spec or "").strip()
    if not s:
        return ""
    s = s.split("#", 1)[0]
    s = s.split("?", 1)[0]
    return s.strip()


def is_relative_spec(spec: str) -> bool:
    s = strip_query_and_hash(spec)
    return s.startswith("./") or s.startswith("../")


def is_absolute_spec(spec: str) -> bool:
    """
    Path-absolute (not URL). '/x/y.js' as used by some bundlers.
    """
    s = strip_query_and_hash(spec)
    return s.startswith("/") and not s.startswith("//")


def is_url_spec(spec: str) -> bool:
    s = (spec or "").strip().lower()
    return s.startswith(("http://", "https://", "data:", "blob:"))


def is_bare_spec(spec: str) -> bool:
    """
    'react', '@scope/pkg', 'lodash/fp' etc.
    """
    s = strip_query_and_hash(spec)
    if not s:
        return False
    return not (is_relative_spec(s) or is_absolute_spec(s) or is_url_spec(s))


def canon_web_file_id(
    s: Posixish | None,
    scan_root: Posixish | None = None,
) -> str:
    """
    Canonical JS/TS file id used for NodeId and edge endpoints:

      - strip query/hash
      - POSIX normalize (including '.'/'..')
      - anchor to scan_root when possible (repo-relative)
      - preserve extension (no invented dotted ids)
    """
    if not s:
        return ""
    raw = strip_query_and_hash(str(s).strip())
    if not raw:
        return ""

    # normalize and relativize
    rel = canon_path_rel(raw, scan_root)

    # remove trailing slashes and leading "./"
    rel = _strip_leading_current_dir(to_posix(rel).rstrip("/"))

    return rel


@dataclass(frozen=True)
class WebResolveResult:
    kind: str  # 'internal' | 'bare' | 'url' | 'unresolved'
    spec: str
    resolved: Optional[str]  # canonical repo-relative id if internal
    tried: tuple[str, ...] = ()


def resolve_web_import(
    spec: str,
    *,
    src_file_id: str,
    scan_root: Posixish | None,
    exists_cb: Callable[[str], bool],
    prefer_exts: tuple[str, ...] = WEB_EXTS,
) -> WebResolveResult:
    """
    Resolve an import specifier to a canonical web file id.

    - Returns internal 'resolved' only when the spec resolves to a repo file.
    - Leaves bare and URL specs as non-internal edges (caller decides).
    - src_file_id should already be canon_web_file_id(...)
    """
    spec0 = (spec or "").strip()
    spec_clean = strip_query_and_hash(spec0)

    if not spec_clean:
        return WebResolveResult(kind="unresolved", spec=spec0, resolved=None)

    if is_url_spec(spec_clean):
        return WebResolveResult(kind="url", spec=spec0, resolved=None)

    if is_bare_spec(spec_clean):
        return WebResolveResult(kind="bare", spec=spec0, resolved=None)

    # src_file_id is repo-relative, so parent is also repo-relative
    src_dir = to_posix(PurePosixPath(src_file_id).parent)
    tried: list[str] = []

    def _emit_try(p: str) -> Optional[str]:
        # p may be repo-relative-ish; normalize and relativize.
        cand = canon_path_rel(p, scan_root).rstrip("/")
        cand = _strip_leading_current_dir(to_posix(cand))
        tried.append(cand)
        if cand and exists_cb(cand):
            return cand
        return None

    # Absolute path import ("/assets/x.js") - treat as repo-root anchored
    if is_absolute_spec(spec_clean):
        base = _strip_leading_current_dir(to_posix(spec_clean.lstrip("/")))

        found = _emit_try(base)
        if found:
            return WebResolveResult(kind="internal", spec=spec0, resolved=found, tried=tuple(tried))

        found = _try_suffix_and_index(base, _emit_try, prefer_exts)
        if found:
            return WebResolveResult(kind="internal", spec=spec0, resolved=found, tried=tuple(tried))

        return WebResolveResult(kind="unresolved", spec=spec0, resolved=None, tried=tuple(tried))

    # Relative spec: join to src_dir and normalize
    base = canon_join_rel(f"{src_dir}/_sentinel.ts", spec_clean)  # join w/ normalization
    # We appended a sentinel file so canon_join_rel uses dirname; remove that:
    # canon_join_rel(dirname("src_dir/_sentinel.ts"), spec) -> correct.
    # The returned base is already normalized.
    found = _emit_try(base)
    if found:
        return WebResolveResult(kind="internal", spec=spec0, resolved=found, tried=tuple(tried))

    found = _try_suffix_and_index(base, _emit_try, prefer_exts)
    if found:
        return WebResolveResult(kind="internal", spec=spec0, resolved=found, tried=tuple(tried))

    return WebResolveResult(kind="unresolved", spec=spec0, resolved=None, tried=tuple(tried))


def _try_suffix_and_index(
    base: str,
    emit_try: Callable[[str], Optional[str]],
    exts: tuple[str, ...],
) -> Optional[str]:
    """
    Given base with no guaranteed extension, try:
      - base + ext
      - base/index + ext   (directory import)
    """
    base = to_posix(base).rstrip("/")
    if not base:
        return None

    # If base already ends with known ext, no need to try suffixes again.
    if has_web_ext(base):
        return None

    for ext in exts:
        found = emit_try(base + ext)
        if found:
            return found

    # Directory import pattern (or missing ext)
    for idx in INDEX_BASENAMES:
        for ext in exts:
            found = emit_try(f"{base}/{idx}{ext}")
            if found:
                return found

    return None


def web_id_variants(token: str, scan_root: Optional[Pathish] = None) -> list[str]:
    """
    Produce plausible variants for fuzzy matching / legacy repair.

      - original (POSIX normalized, query/hash stripped, normpath applied)
      - canon_web_file_id(token)
      - if token looks like '/foo/bar.js', also emit 'foo/bar.js'

    Unlike Python id_variants, we do NOT generate dotted ids.
    """
    out: list[str] = []
    seen: set[str] = set()

    def _emit(v: Optional[str]) -> None:
        if not v:
            return
        s = to_posix(strip_query_and_hash(v)).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)

    _emit(token)
    _emit(canon_web_file_id(token, scan_root))

    t = strip_query_and_hash(token or "")
    t = t.replace("\\", "/")
    if t.startswith("/") and not t.startswith("//"):
        _emit(t.lstrip("/"))

    return out
