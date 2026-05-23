# backend/saas_analyzer/analyzer/ts/resolve_imports.py
from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Iterable, Optional, Sequence
import posixpath

from .canonical_web import (
    canon_web_file_id,
    canon_path_rel,
    canon_join_rel,
    strip_query_and_hash,
    to_posix,
    WEB_EXTS,
)

# tsconfig path mapping support (baseline-first, nearest-fallback handled by caller via `pathmaps`)
from .tsconfig_paths import TSPathMap, expand_ts_paths

TS_EXTS = WEB_EXTS  # (".js",".mjs",".cjs",".jsx",".ts",".tsx") order as defined in SSOT


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


def _norm_repo_rel(p: str) -> str:
    """
    Normalize a repo-relative POSIX path string:
      - strip leading "./" and one leading "/"
      - posix normpath (collapses '.', '..', extra slashes)
    Does NOT ensure safety; caller should still enforce repo-root containment.
    """
    p = _strip_leading_current_dir(p)
    p = to_posix(p)
    p = posixpath.normpath(p)
    if p == ".":
        return ""
    return p

def _candidate_rel_paths(base_rel_posix: str) -> Iterable[str]:
    base_rel_posix = _norm_repo_rel(to_posix(base_rel_posix).rstrip("/"))
    if not base_rel_posix:
        return

    yield base_rel_posix

    p = PurePosixPath(base_rel_posix)
    
    # ESM-style .js imports that point to .ts source files
    # e.g. import './actions.js' where the actual file is actions.ts
    if p.suffix == ".js":
        stem = str(p.parent / p.stem) if str(p.parent) != "." else p.stem
        # handle parent dir properly
        stem = str(PurePosixPath(base_rel_posix).with_suffix(""))
        yield stem + ".ts"
        yield stem + ".tsx"
        yield stem + "/index.ts"
        yield stem + "/index.tsx"

    elif p.suffix == "":
        # existing logic unchanged
        for ext in TS_EXTS:
            yield str(PurePosixPath(base_rel_posix + ext))
        base_dir = base_rel_posix.rstrip("/")
        for ext in TS_EXTS:
            yield f"{base_dir}/index{ext}"

def _resolve_via_disk(
    *,
    repo_root: Path,
    src_rel_posix: str,
    spec: str,
    treat_slash_root_as_repo_root: bool,
) -> Optional[str]:
    """
    Internal helper: resolve only relative specs and (optionally) '/root' specs,
    using extension/index probing and SSOT canonical ids.

    This is factored so tsconfig alias candidates can reuse the same logic.
    """
    if not spec:
        return None

    spec_clean = strip_query_and_hash(spec)
    if not spec_clean:
        return None

    # Only handle relative or /root here.
    is_rel = spec_clean.startswith("./") or spec_clean.startswith("../")
    is_root = treat_slash_root_as_repo_root and spec_clean.startswith("/") and not spec_clean.startswith("//")
    if not (is_rel or is_root):
        return None

    repo_root_abs = repo_root.resolve()

    # Canonicalize src to SSOT id (repo-relative POSIX when possible)
    src_id = canon_web_file_id(src_rel_posix, repo_root_abs)
    if not src_id:
        return None

    # Defensive: src_id should be repo-relative, not URL-ish or absolute.
    if src_id.startswith("/") or "://" in src_id:
        return None

    # Build a normalized repo-relative base candidate
    if is_root:
        # /root spec: treat as repo-root anchored
        base_rel = _norm_repo_rel(spec_clean.lstrip("/"))
    else:
        # relative spec: join against canonical src id
        base_rel = canon_join_rel(src_id, spec_clean)

    # Final normalize / relativize safety
    base_rel = canon_path_rel(base_rel, repo_root_abs)
    base_rel = _norm_repo_rel(base_rel)

    # Reject anything that still escapes repo-root after normalization
    if base_rel.startswith("../") or base_rel == "..":
        return None

    # Enumerate candidates and check existence
    for cand_rel in _candidate_rel_paths(base_rel):
        cand_rel = _norm_repo_rel(cand_rel)
        if not cand_rel:
            continue

        # Avoid probing outside repo_root
        if cand_rel.startswith("../") or cand_rel == "..":
            continue

        cand_abs = (repo_root_abs / cand_rel).resolve()
        try:
            cand_abs.relative_to(repo_root_abs)
        except Exception:
            continue

        if cand_abs.is_file():
            return canon_web_file_id(cand_rel, repo_root_abs)

    return None


def _resolve_via_tsconfig_aliases(
    *,
    repo_root: Path,
    src_rel_posix: str,
    spec_clean: str,
    pathmaps: Sequence[TSPathMap],
) -> Optional[str]:
    """
    Try tsconfig alias expansion against one or more pathmaps in order.
    Returns the first resolved internal file id, else None.

    The caller supplies fallback order (baseline-first, nearest-fallback).
    """
    if not pathmaps:
        return None

    for pm in pathmaps:
        if not pm or not pm.paths:
            continue

        candidates = expand_ts_paths(pm=pm, spec=spec_clean)
        for cand in candidates:
            # Feed candidate through the same disk resolver as a repo-root spec.
            dst2 = _resolve_via_disk(
                repo_root=repo_root,
                src_rel_posix=src_rel_posix,
                spec="/" + cand,
                treat_slash_root_as_repo_root=True,
            )
            if dst2:
                return dst2

    return None


def resolve_specifier_to_file(
    *,
    repo_root: Path,
    src_rel_posix: str,
    spec: str,
    treat_slash_root_as_repo_root: bool = True,
    pathmap: Optional[TSPathMap] = None,
    pathmaps: Optional[Sequence[TSPathMap]] = None,
) -> Optional[str]:
    """
    Resolve a JS/TS import specifier to a canonical repo-relative POSIX file-id
    if it points at an in-repo file. Returns None if unresolved.

    Resolves:
      1) relative specs (./, ../)
      2) optional "/" root specs (if enabled)
      3) tsconfig path aliases via compilerOptions.paths/baseUrl (for pre-TS 7.0 codebase)

    Current approach:
      - Always do disk resolution first (relative + /root).
      - Alias resolution is baseline-first with nearest-config as fallback,
        controlled by the caller via `pathmaps` order.
      - `pathmap` remains supported for backwards compatibility.

    Notes:
      - We still ONLY return in-repo file ids (SSOT canonical).
      - Bare package imports remain None (caller tracks as external/unresolved).
    """
    if not spec:
        return None

    spec_clean = strip_query_and_hash(spec)
    if not spec_clean:
        return None

    # 1/2) Disk resolution for relative and /root
    dst = _resolve_via_disk(
        repo_root=repo_root,
        src_rel_posix=src_rel_posix,
        spec=spec_clean,
        treat_slash_root_as_repo_root=treat_slash_root_as_repo_root,
    )
    if dst:
        return dst

    # If disk resolution didn't apply or failed, only attempt alias expansion for
    # "bare-ish" specs. Do NOT attempt aliases for relative, /root, or URL-like specs.
    low = spec_clean.strip().lower()
    if low.startswith("./") or low.startswith("../"):
        return None
    if low.startswith("//"):
        return None
    if low.startswith("/"):
        # Even if treat_slash_root_as_repo_root=False, this is still a /root-style spec,
        # not a tsconfig alias.
        return None
    if low.startswith("http://") or low.startswith("https://") or low.startswith("data:") or low.startswith("blob:"):
        return None

    # 3) tsconfig alias resolution for non-relative, non-/root specs
    # Prefer ordered fallback list if provided (baseline-first strategy).
    if pathmaps is not None:
        return _resolve_via_tsconfig_aliases(
            repo_root=repo_root,
            src_rel_posix=src_rel_posix,
            spec_clean=spec_clean,
            pathmaps=pathmaps,
        )

    # Back-compat: single pathmap
    if pathmap and pathmap.paths:
        return _resolve_via_tsconfig_aliases(
            repo_root=repo_root,
            src_rel_posix=src_rel_posix,
            spec_clean=spec_clean,
            pathmaps=(pathmap,),
        )

    return None
