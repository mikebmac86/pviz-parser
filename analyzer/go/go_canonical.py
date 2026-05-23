# backend/saas_analyzer/analyzer/go/canonical_go.py
from __future__ import annotations

"""
Go canonicalizer for PViz.

This module is the single source of truth for:
  - classification of Go imports (stdlib/module/local/relative/unresolved)
  - mapping import specs into internal package-id space (module import paths)
  - deterministic prefix-collapse behavior (nearest known internal package)
  - producing internal edges from a Go FolderIndex (deduped + sorted)

Design notes:
  - Go import specs are package paths; no filesystem probing here.
  - "internal" means: resolves to one of the known internal package ids.
  - "local" means: module_path prefix, but not found in internal ids.
  - This is intentionally tolerant: it never raises on bad inputs.
"""

from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GoResolveResult:
    """
    TS-like resolver feel, but for Go package import specs.

    kind:
      - 'internal'   resolved to a known internal package id
      - 'local'      module_path prefix but not known internal id
      - 'stdlib'     looks like stdlib (no dot in first path segment)
      - 'module'     third-party module import (dot in first segment)
      - 'relative'   starts with ./ or ../ (rare/invalid in normal Go imports)
      - 'unresolved' empty/invalid or could not map to internal

    resolved:
      - canonical internal package id when kind == 'internal'
      - otherwise None

    tried:
      - candidates attempted during prefix collapse (deterministic)
    """
    kind: str
    spec: str
    resolved: Optional[str]
    tried: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Canonicalization helpers (single source of truth)
# ---------------------------------------------------------------------------

_QUOTE_CHARS = ("'", '"', "`")


def normalize_import_spec(spec: str) -> str:
    """
    Normalize a raw import spec string into a stable comparable key.

    Handles:
      - trimming whitespace
      - removing surrounding quotes/backticks (common in parsed tokens)
      - normalizing path separators to '/'
      - collapsing duplicate slashes
      - stripping leading/trailing slashes
    """
    s = (spec or "").strip()
    if not s:
        return ""

    # Strip surrounding quotes/backticks.
    # Be tolerant of mismatched ends (e.g. '"x`' from tokenization glitches).
    if len(s) >= 2 and s[0] in _QUOTE_CHARS and s[-1] in _QUOTE_CHARS:
        # Prefer the strict same-char case first.
        if s[0] == s[-1]:
            s = s[1:-1].strip()
        else:
            # Strip one char from both ends (best-effort).
            s = s[1:-1].strip()
    elif len(s) >= 2 and s[0] in _QUOTE_CHARS and s[-1] == s[0]:
        s = s[1:-1].strip()

    s = s.replace("\\", "/")

    # Collapse multiple slashes (stable, small loops)
    while "//" in s:
        s = s.replace("//", "/")

    # Strip leading/trailing slashes only (do NOT strip "./" or "../")
    s = s.strip("/")
    return s


def normalize_module_path(module_path: Optional[str]) -> Optional[str]:
    mp = normalize_import_spec(module_path or "")
    return mp or None


def _looks_normalized(s: str) -> bool:
    """
    Heuristic: decide if a string is already in our normalized import-spec form.
    Used to avoid normalizing large internal-id sets on every resolve call.
    """
    if not s:
        return True
    if any(q in s for q in _QUOTE_CHARS):
        return False
    if "\\" in s:
        return False
    if "//" in s:
        return False
    if s.startswith("/") or s.endswith("/"):
        return False
    # We allow "./" or "../" here; normalize_import_spec preserves them.
    return True


def _ensure_normalized_internal_ids(internal_pkg_ids: Iterable[str]) -> set[str]:
    """
    internal_pkg_ids are *supposed* to be pre-normalized by callers.

    In practice, long-lived workers + mixed call sites can feed partially
    normalized sets. This helper:
      - samples a few entries
      - uses the incoming iterable as-is if it looks normalized
      - otherwise normalizes everything into a new set

    Returns a set[str] suitable for fast membership tests.
    """
    # If already a set, we may be able to use it directly.
    if isinstance(internal_pkg_ids, set):
        s = internal_pkg_ids
        # Sample up to 8 items to decide.
        i = 0
        for v in s:
            if not isinstance(v, str):
                return {normalize_import_spec(str(x)) for x in s if normalize_import_spec(str(x))}
            if not _looks_normalized(v):
                return {normalize_import_spec(x) for x in s if normalize_import_spec(x)}
            i += 1
            if i >= 8:
                break
        return s

    # General iterable: normalize into a set
    out: set[str] = set()
    for v in internal_pkg_ids:
        if not isinstance(v, str):
            v = str(v)
        nv = normalize_import_spec(v)
        if nv:
            out.add(nv)
    return out


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify_go_import(spec: str, *, module_path: Optional[str]) -> str:
    """
    Basic Go import classification consistent with FolderIndex behavior.

    Inputs are normalized internally.
    """
    s = normalize_import_spec(spec)
    if not s:
        return "unresolved"

    if s.startswith(("./", "../")):
        return "relative"

    mp = normalize_module_path(module_path)
    if mp and (s == mp or s.startswith(mp + "/")):
        return "local"

    first = s.split("/", 1)[0]
    if "." in first:
        return "module"
    return "stdlib"


# ---------------------------------------------------------------------------
# Resolution (internal pkg-id mapping)
# ---------------------------------------------------------------------------

def resolve_go_import(
    spec: str,
    *,
    module_path: Optional[str],
    internal_pkg_ids: set[str] | Iterable[str],
    allow_prefix_collapse: bool = True,
) -> GoResolveResult:
    """
    Resolve a Go import spec into *internal package-id space*.

    Rules:
      - If normalized spec matches an internal package id exactly -> internal
      - Else if allow_prefix_collapse: progressively drop tail segments until a match is found
        (a/b/c -> a/b -> a)
      - Otherwise return a classified non-internal kind.

    Notes:
      - No filesystem probing or existence checks.
      - Deterministic: 'tried' is stable order of attempted candidates.
      - Defensively normalizes `internal_pkg_ids` when it does not appear normalized.
    """
    spec0 = spec or ""
    s = normalize_import_spec(spec0)
    if not s:
        return GoResolveResult(kind="unresolved", spec=spec0, resolved=None, tried=())

    internal = _ensure_normalized_internal_ids(internal_pkg_ids)

    if s in internal:
        return GoResolveResult(kind="internal", spec=spec0, resolved=s, tried=(s,))

    tried: list[str] = [s]

    if allow_prefix_collapse:
        parts = s.split("/")
        while len(parts) > 1:
            parts.pop()
            cand = "/".join(parts)
            tried.append(cand)
            if cand in internal:
                return GoResolveResult(kind="internal", spec=spec0, resolved=cand, tried=tuple(tried))

    kind = classify_go_import(s, module_path=module_path)
    return GoResolveResult(kind=kind, spec=spec0, resolved=None, tried=tuple(tried))


def resolve_many(
    specs: Sequence[str],
    *,
    module_path: Optional[str],
    internal_pkg_ids: set[str] | Iterable[str],
    allow_prefix_collapse: bool = True,
) -> list[GoResolveResult]:
    """
    Resolve many specs deterministically (preserves input order).
    """
    out: list[GoResolveResult] = []
    for raw in (specs or ()):
        out.append(
            resolve_go_import(
                raw,
                module_path=module_path,
                internal_pkg_ids=internal_pkg_ids,
                allow_prefix_collapse=allow_prefix_collapse,
            )
        )
    return out


# ---------------------------------------------------------------------------
# FolderIndex integration helpers
# ---------------------------------------------------------------------------

def internal_pkg_ids_from_folder_index(idx: Any) -> set[str]:
    """
    Build a normalized internal package-id set from a Go FolderIndex.

    FolderIndex convention:
      - idx.files is a mapping where each value has FileEntry.id = package id.
    """
    out: set[str] = set()
    files = getattr(idx, "files", {}) or {}
    for _k, fe in files.items():
        fid = getattr(fe, "id", None)
        if isinstance(fid, str):
            s = normalize_import_spec(fid)
            if s:
                out.add(s)
    return out


def build_internal_edges_from_folder_index(
    idx: Any,
    *,
    drop_self_edges: bool = True,
) -> list[tuple[str, str]]:
    """
    Produce unique internal (src_pkg_id, dst_pkg_id) edges directly from FolderIndex,
    using FileEntry.imports_internal (already canonical package ids).

    Returns a stable sorted list.
    """
    edges: set[tuple[str, str]] = set()
    files = getattr(idx, "files", {}) or {}
    for _k, fe in files.items():
        src0 = getattr(fe, "id", None)
        if not isinstance(src0, str):
            continue
        src = normalize_import_spec(src0)
        if not src:
            continue

        imps = getattr(fe, "imports_internal", None) or ()
        for dst0 in imps:
            if not isinstance(dst0, str):
                continue
            dst = normalize_import_spec(dst0)
            if not dst:
                continue
            if drop_self_edges and dst == src:
                continue
            edges.add((src, dst))

    return sorted(edges)
