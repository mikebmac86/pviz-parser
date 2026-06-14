# backend/saas_analyzer/analyzer/go/folder_index.py
from __future__ import annotations

"""
FolderIndex — deterministic snapshot of a workspace's Go files.

Schema: FOLDER_INDEX_SCHEMA (analyzer_store.types)

Design mirrors analyzer_store/folder_index.py for Python:
  - Deterministic: stable sorting, deduped lists
  - Error tolerant: parse_status captures failures; facts may be partial
  - imports_all: raw import tokens (Go import paths)
  - imports_internal: recomputed after normalization, based on internal package ids
  - IDs are *package import paths* (module-id space) for FileEntry.id and FolderIndex.files keys
  - FileEntry.file remains repo-relative POSIX file path
  - Atomic writes through analyzer_store.io_utils.atomic_write_json

Go-specific notes:
  - "module-id" == Go package import path (e.g. "github.com/acme/app/internal/db")
  - internal import resolution uses go.mod module path + directory scan (no `go list` yet)

UPDATED: Now handles monorepos where go.mod is in a subdirectory (e.g., backend/go.mod)

UPDATED (mandatory Go AST cache):
  - goextract batch parse is REQUIRED for FolderIndex build (mirrors Java posture).
  - imports_all, parse_status, error_snippet, and AST-side loc_code are derived from goextract cache.
  - NodeFacts and other downstream builders should reuse the same cache (no reparse).

UPDATED (metrics consistency):
  - FileEntry.loc is always physical LOC from local text metrics.
  - FileEntry.sloc is code/SLOC-like LOC, optionally using goextract loc_code when present.
  - comment_lines, blank_lines, and comment_pct are based on physical LOC.

UPDATED (v1.2/v1.9-compatible):
  - imports_all preserves raw Go import paths.
  - imports_internal preserves resolved internal package ids.
  - imports_external preserves unresolved external import paths.
  - language_facts["go"] carries Go-specific parser/package facts.  
"""

from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple
import os
import re

from analyzer_store.types import FOLDER_INDEX_SCHEMA, FileEntry, FolderIndex
from analyzer_store.io_utils import iso_utc, blake2s_short, atomic_write_json, load_json_bytes

# Canonical helpers (project)
from adapters.canonical import repo_rel, to_posix  # keep consistent with python folder index

# SINGLE SOURCE OF TRUTH for Go normalization/resolution:
from analyzer.go.go_canonical import (
    classify_go_import,
    normalize_import_spec,
    normalize_module_path,
    resolve_go_import,
)

# Go AST cache (goextract batch)
from analyzer.go.go_parse_dispatch import get_go_batch_parser

# Diagnostics logging
try:
    from diagnostics.logging import log_event
except Exception:  # pragma: no cover
    def log_event(*_a, **_k):
        return


# ---------------------------------------------------------------------------
# Go helpers
# ---------------------------------------------------------------------------

_GO_IMPORT_RE = re.compile(r'^\s*import\s*(\(|")', re.MULTILINE)

def _tuple_strs(value: object) -> Tuple[str, ...]:
    """
    Normalize a JSON-loaded sequence into tuple[str, ...].
    Tolerates older artifacts with missing/null/scalar fields.
    """
    if value is None:
        return tuple()

    if isinstance(value, str):
        s = value.strip()
        return (s,) if s else tuple()

    if isinstance(value, (list, tuple, set, frozenset)):
        out: List[str] = []
        for item in value:
            if item is None:
                continue
            s = str(item).strip()
            if s:
                out.append(s)
        return tuple(out)

    s = str(value).strip()
    return (s,) if s else tuple()


def _mapping_or_empty(value: object) -> Mapping[str, object]:
    if isinstance(value, dict):
        return value
    return {}

def _find_go_mod(repo_root: Path, *, go_files: Optional[Sequence[Path]] = None) -> Optional[Path]:
    """
    Find go.mod in repo (handles monorepos) in a deterministic, file-aware way.

    Priority:
      A) If any go.mod exists at repo_root or above, prefer the nearest ancestor go.mod
         (standard single-module repo / nested workspace usage).

      B) Otherwise (monorepo with go.mod under repo_root):
         - Enumerate ALL go.mod under repo_root.
         - If go_files is provided, score go.mod candidates by:
             1) number of provided go_files that lie under that go.mod's directory (coverage)
             2) go.mod directory depth (prefer deeper / more specific module)
             3) stable lexical tie-breaker (posix path)
         - If go_files is empty/unavailable, fall back to choosing the deepest go.mod,
           then lexical tie-breaker.

    Returns:
      Selected go.mod path or None.
    """
    repo_root = repo_root.resolve()

    # A) Upward search (standard case): nearest go.mod at/above repo_root
    for parent in [repo_root] + list(repo_root.parents):
        gm = parent / "go.mod"
        if gm.exists() and gm.is_file():
            return gm

    # B) Downward search (monorepo): evaluate all go.mod under repo_root
    candidates: List[Path] = []
    try:
        for gm in repo_root.rglob("go.mod"):
            try:
                if gm.is_file():
                    candidates.append(gm.resolve())
            except Exception:
                continue
    except Exception:
        candidates = []

    if not candidates:
        return None

    # Normalize go_files to absolute paths (best-effort)
    files_abs: List[Path] = []
    for p in (go_files or ()):
        try:
            ap = Path(p).resolve(strict=False)
        except Exception:
            continue
        if ap.suffix.lower() == ".go":
            files_abs.append(ap)

    def _posix(p: Path) -> str:
        try:
            return p.as_posix()
        except Exception:
            return str(p)

    # If we have file context, pick the module that "covers" the most Go files.
    if files_abs:
        best: Optional[Path] = None
        best_cov = -1
        best_depth = -1
        best_key = ""

        for gm in candidates:
            mod_dir = gm.parent
            cov = 0
            for f in files_abs:
                try:
                    f.relative_to(mod_dir)
                    cov += 1
                except Exception:
                    continue

            try:
                rel = mod_dir.relative_to(repo_root)
                depth = len(rel.parts)
            except Exception:
                depth = 0

            key = _posix(gm)

            # Sort by: coverage desc, depth desc, path asc
            if (
                cov > best_cov
                or (cov == best_cov and depth > best_depth)
                or (cov == best_cov and depth == best_depth and key < best_key)
            ):
                best = gm
                best_cov = cov
                best_depth = depth
                best_key = key

        if best is not None and best_cov > 0:
            return best

    # No file context or no coverage: choose deepest go.mod under repo_root deterministically
    def _depth(gm: Path) -> int:
        try:
            rel = gm.parent.relative_to(repo_root)
            return len(rel.parts)
        except Exception:
            return 0

    candidates_sorted = sorted(candidates, key=lambda gm: (-_depth(gm), _posix(gm)))
    return candidates_sorted[0]


def _read_go_module_path(go_mod_path: Path) -> Optional[str]:
    """
    Parse `module <path>` from go.mod.
    Very small parser; good enough for most repos.
    """
    try:
        txt = go_mod_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None

    for raw in txt.splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if line.startswith("module "):
            return line[len("module ") :].strip()
    return None


def _iter_go_files(root: Path, *, include_tests: bool = True) -> List[Path]:
    """Deterministic, de-duped list of .go files under root."""
    root = root.resolve()
    out: List[Path] = []
    try:
        for p in root.rglob("*.go"):
            if not p.is_file():
                continue
            if not include_tests and p.name.endswith("_test.go"):
                continue
            out.append(p.resolve())
    except Exception:
        pass

    return sorted(set(out), key=lambda p: to_posix(str(p)))


def _count_go_metrics(text: str) -> Tuple[int, int, int, int, Optional[float]]:
    """
    Compute normalized metrics for Go.

    Returns:
        loc, sloc, comment_lines, blank_lines, comment_pct

    loc:
        Physical line count.

    sloc:
        Lines containing code after stripping comments.

    comment_lines:
        Comment-only physical lines.

    blank_lines:
        Blank / whitespace-only physical lines.

    comment_pct:
        comment_lines / physical loc.
    """
    loc = 0
    sloc = 0
    comment_lines = 0
    blank_lines = 0
    in_block = False

    for raw in text.splitlines():
        loc += 1

        stripped = raw.strip()
        if not stripped:
            blank_lines += 1
            continue

        i = 0
        n = len(raw)
        code_chars: List[str] = []
        saw_comment = False

        while i < n:
            if in_block:
                saw_comment = True
                end = raw.find("*/", i)
                if end == -1:
                    i = n
                    break
                in_block = False
                i = end + 2
                continue

            if raw.startswith("/*", i):
                in_block = True
                saw_comment = True
                i += 2
                continue

            if raw.startswith("//", i):
                saw_comment = True
                break

            code_chars.append(raw[i])
            i += 1

        has_code = bool("".join(code_chars).strip())

        if has_code:
            sloc += 1
        elif saw_comment:
            comment_lines += 1

    comment_pct = (comment_lines / loc) if loc else None
    return loc, sloc, comment_lines, blank_lines, comment_pct


def _parse_go_imports_and_package(text: str) -> Tuple[Optional[str], List[str]]:
    """
    Minimal Go parser for:
      - package name
      - import specs

    NOTE: Kept for backwards compatibility / debugging, but FolderIndex imports are
    now sourced from the goextract cache (mandatory AST).
    """
    pkg: Optional[str] = None
    imports: List[str] = []

    # package name (first non-comment package decl)
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if line.startswith("package "):
            pkg = line[len("package ") :].strip().split()[0]
            break

    if not _GO_IMPORT_RE.search(text):
        return pkg, imports

    # Parse imports (single or block)
    in_block = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue

        if not in_block and line.startswith("import "):
            rest = line[len("import ") :].strip()
            if rest.startswith("("):
                in_block = True
                continue

            q1 = rest.find('"')
            if q1 != -1:
                q2 = rest.find('"', q1 + 1)
                if q2 != -1:
                    spec = rest[q1 + 1 : q2].strip()
                    if spec:
                        imports.append(spec)
            continue

        if in_block:
            if line.startswith(")"):
                in_block = False
                continue

            q1 = line.find('"')
            if q1 != -1:
                q2 = line.find('"', q1 + 1)
                if q2 != -1:
                    spec = line[q1 + 1 : q2].strip()
                    if spec:
                        imports.append(spec)

    seen: Set[str] = set()
    uniq: List[str] = []
    for s in imports:
        if s not in seen:
            seen.add(s)
            uniq.append(s)

    return pkg, uniq


def _build_go_package_index(
    *,
    root: Path,
    module_path: Optional[str],
    go_files: List[Path],
    go_mod_dir: Optional[Path] = None,
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """
    Build:
      - dir_rel_posix (STRIPPED) -> package_import_path
      - package_import_path -> representative file (repo-relative posix)

    Monorepo support:
      - if go.mod is under a subdir, strip that prefix from directory keys and package ids
        so ids become: <module_path>/<dir_under_module> (not duplicated).

    CRITICAL:
      - dir_to_pkg keys MUST match what _logical_go_pkg_for_file computes (after stripping),
        otherwise package-id lookups will fall back and can drift.
    """
    dir_to_pkg: Dict[str, str] = {}
    pkg_to_repfile: Dict[str, str] = {}

    mp = normalize_module_path(module_path)
    if not mp:
        return dir_to_pkg, pkg_to_repfile

    by_dir: Dict[str, List[Path]] = {}
    for p in go_files:
        try:
            rel = to_posix(str(p.resolve().relative_to(root.resolve())))
        except Exception:
            rel = to_posix(str(p))
        d = to_posix(str(Path(rel).parent))
        by_dir.setdefault(d, []).append(p)

    go_mod_rel = None
    if go_mod_dir:
        try:
            go_mod_rel = to_posix(str(go_mod_dir.relative_to(root))).strip("/")
        except Exception:
            go_mod_rel = None

    for d, paths in by_dir.items():
        d_original = d.strip().strip("/")
        d_stripped = d_original

        if go_mod_rel:
            if d_original.startswith(go_mod_rel + "/"):
                d_stripped = d_original[len(go_mod_rel) + 1 :]
                log_event("GO_PKG_INDEX:stripped", original=d_original, stripped=d_stripped)
            elif d_original == go_mod_rel:
                d_stripped = ""
                log_event("GO_PKG_INDEX:stripped_root", original=d_original)

        d_key = normalize_import_spec(d_stripped)
        if d_key == ".":
            d_key = ""

        pkg = mp if not d_key else f"{mp}/{d_key}"
        pkg = normalize_import_spec(pkg)

        dir_to_pkg[d_key or ""] = pkg

        rel_files: List[str] = []
        for p in sorted(paths, key=lambda x: to_posix(str(x))):
            try:
                rel_files.append(to_posix(str(p.resolve().relative_to(root.resolve()))))
            except Exception:
                rel_files.append(to_posix(str(p)))

        rep = None
        for rf in rel_files:
            if rf.endswith("/doc.go") or Path(rf).name == "doc.go":
                rep = rf
                break

        if rep is None:
            for rf in rel_files:
                if not rf.endswith("_test.go"):
                    rep = rf
                    break

        if rep is None and rel_files:
            rep = rel_files[0]

        if rep:
            pkg_to_repfile[pkg] = rep

    return dir_to_pkg, pkg_to_repfile


def _logical_go_pkg_for_file(
    *,
    root: Path,
    rel_file_posix: str,
    module_path: Optional[str],
    dir_to_pkg: Mapping[str, str],
    go_mod_dir: Optional[Path] = None,
) -> str:
    """
    FileEntry.id key-space for Go: package import path.

    Must match _build_go_package_index’s stripping logic.
    """
    rel = normalize_import_spec(rel_file_posix).lstrip("/")
    d = normalize_import_spec(to_posix(str(Path(rel).parent)))

    if d == ".":
        d = ""

    mp = normalize_module_path(module_path)

    go_mod_rel = None
    if go_mod_dir:
        try:
            go_mod_rel = normalize_import_spec(to_posix(str(go_mod_dir.relative_to(root))).strip("/"))
        except Exception:
            go_mod_rel = None

    if go_mod_rel and d:
        if d.startswith(go_mod_rel + "/"):
            d = d[len(go_mod_rel) + 1 :]
        elif d == go_mod_rel:
            d = ""

        d = normalize_import_spec(d)

        if d == ".":
            d = ""

    pkg_from_map = dir_to_pkg.get(d or "")
    fallback_pkg = (mp if not d else f"{mp}/{d}") if mp else (d if d else ".")

    if mp:
        result = pkg_from_map if pkg_from_map else fallback_pkg
        result = normalize_import_spec(result)

        if result == f"{mp}/.":
            result = mp

        return result

    return fallback_pkg


def _require_goextract_loaded(*, go_files: Sequence[Path], cfg: object) -> None:
    """
    Mandatory goextract batch parse.

    This intentionally raises on failure (Java-like posture).
    """
    files_abs: List[Path] = []
    for p in (go_files or ()):
        try:
            ap = Path(p).resolve(strict=False)
        except Exception:
            continue
        if ap.suffix.lower() == ".go":
            files_abs.append(ap)

    files_abs = sorted(set(files_abs), key=lambda x: to_posix(str(x)))
    if not files_abs:
        return

    include_docs = bool(getattr(cfg, "goextract_include_docs", True))
    include_imports = bool(getattr(cfg, "goextract_include_imports", True))
    include_build = bool(getattr(cfg, "goextract_include_build", True))
    timeout_s = int(getattr(cfg, "goextract_timeout_s", 180) or 180)
    batch_size = int(getattr(cfg, "goextract_batch_size", 0) or 0)
    goextract_bin = getattr(cfg, "goextract_bin", None)

    bp = get_go_batch_parser()

    log_event(
        "GO_FOLDER_INDEX:goextract_preflight",
        files=len(files_abs),
        cfg_goextract_bin=str(goextract_bin or ""),
        env_goextract_bin=str(os.getenv("PVIZ_GOEXTRACT_BIN", "") or ""),
        helper_bin=str(getattr(bp, "helper_bin", "") or ""),
    )

    if goextract_bin:
        try:
            bp.helper_bin = Path(goextract_bin)  # type: ignore[attr-defined]
        except Exception:
            pass

    if batch_size and batch_size > 0:
        for i in range(0, len(files_abs), batch_size):
            bp.load_batch(
                files_abs[i : i + batch_size],
                include_docs=include_docs,
                include_imports=include_imports,
                include_build=include_build,
                timeout_s=timeout_s,
            )
    else:
        bp.load_batch(
            files_abs,
            include_docs=include_docs,
            include_imports=include_imports,
            include_build=include_build,
            timeout_s=timeout_s,
        )

    log_event(
        "GO_FOLDER_INDEX:goextract_loaded",
        files=len(files_abs),
        docs=bool(include_docs),
        imports=bool(include_imports),
        build=bool(include_build),
        chunk_size=int(batch_size or 0),
        helper_bin=str(getattr(bp, "helper_bin", "") or ""),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_folder_index(root: Path, cfg, files: Optional[Sequence[Path]] = None) -> FolderIndex:
    """
    Build FolderIndex for Go.

    Args:
        root: Repository root path
        cfg: Configuration object (uses cfg.max_file_bytes if present)
        files: Optional pre-filtered file list for bucket mode.
    """
    log_event("GO_FOLDER_INDEX:build_enter", root=str(root))

    root = Path(root).resolve()

    if files is None:
        go_files = _iter_go_files(root, include_tests=True)
        log_event("GO_FOLDER_INDEX:iter_files_end", root=str(root), count=len(go_files), mode="full_repo")
    else:
        go_files_filtered: List[Path] = []
        skipped_outside = 0
        skipped_non_go = 0

        for p in files:
            try:
                ap = p.resolve()
            except Exception:
                continue

            if not ap.exists() or not ap.is_file():
                continue

            if ap.suffix.lower() != ".go":
                skipped_non_go += 1
                continue

            try:
                ap.relative_to(root)
                go_files_filtered.append(ap)
            except Exception:
                skipped_outside += 1

        go_files = sorted(set(go_files_filtered), key=lambda p: to_posix(str(p)))
        log_event(
            "GO_FOLDER_INDEX:iter_files_end",
            root=str(root),
            count=len(go_files),
            mode="bucket",
            skipped_outside=skipped_outside,
            skipped_non_go=skipped_non_go,
        )

    go_mod = _find_go_mod(root, go_files=go_files)
    module_path_raw = _read_go_module_path(go_mod) if go_mod else None
    module_path = normalize_module_path(module_path_raw)
    go_mod_dir = go_mod.parent if go_mod else None

    log_event(
        "GO_FOLDER_INDEX:module_path",
        found=bool(module_path),
        module=str(module_path or ""),
        go_mod=str(to_posix(go_mod.relative_to(root)) if go_mod else ""),
    )

    dir_to_pkg, pkg_to_repfile = _build_go_package_index(
        root=root,
        module_path=module_path,
        go_files=go_files,
        go_mod_dir=go_mod_dir,
    )

    log_event(
        "GO_FOLDER_INDEX:package_index_built",
        dirs=len(dir_to_pkg),
        repfiles=len(pkg_to_repfile),
    )

    # -----------------------------------------------------------------------
    # Mandatory goextract batch parse (cache warmup)
    # -----------------------------------------------------------------------
    _require_goextract_loaded(go_files=go_files, cfg=cfg)
    bp = get_go_batch_parser()

    files_map: Dict[str, FileEntry] = {}
    parse_issues: List[str] = []

    max_bytes = getattr(cfg, "max_file_bytes", None)

    for path in go_files:
        parse_status = "ok"
        err_snip: Optional[str] = None
        imports_all_set: Set[str] = set()
        imports_internal: Tuple[str, ...] = tuple()

        # Keep schema meanings stable:
        #   loc = physical LOC
        #   sloc = code/SLOC-like LOC
        loc: Optional[int] = None
        sloc: Optional[int] = None
        comment_lines: Optional[int] = None
        blank_lines: Optional[int] = None
        comment_pct: Optional[float] = None
        goextract_loc_code: Optional[int] = None

        size_bytes: Optional[int] = None
        mtime_iso: Optional[str] = None
        short_hash: Optional[int] = None
        eligible = True

        rel_file = to_posix(repo_rel(path, root))
        abs_path = path.resolve(strict=False)

        # Pull goextract record (mandatory posture: cache miss is an error)
        gp = None
        try:
            gp = bp.get(abs_path)
        except Exception:
            gp = None

        if gp is None:
            parse_status = "error"
            err_snip = "goextract_cache_miss"
            parse_issues.append(f"{rel_file}: goextract_cache_miss")
        else:
            try:
                ps = str(getattr(gp, "parse_status", "") or "").strip().lower()
                if ps in ("ok", "warn", "error"):
                    parse_status = ps
                else:
                    parse_status = "warn" if ps else "warn"
            except Exception:
                parse_status = "warn"

            try:
                es = getattr(gp, "error_snippet", None)
                err_snip = (str(es)[:200] if es else None)
            except Exception:
                err_snip = None

            # goextract loc_code is code-ish LOC, not physical LOC.
            # Keep it as an optional SLOC override rather than assigning it to loc.
            try:
                lc = getattr(gp, "loc_code", None)
                goextract_loc_code = int(lc) if lc is not None else None
            except Exception:
                goextract_loc_code = None

            try:
                imps = getattr(gp, "imports", None) or []
                for rec in imps:
                    if not isinstance(rec, dict):
                        continue
                    raw_spec = rec.get("path") if "path" in rec else rec.get("Path")
                    spec = normalize_import_spec(str(raw_spec or ""))
                    if spec:
                        imports_all_set.add(spec)
            except Exception:
                pass

        # File stats + SLOC from text.
        # These definitions should remain stable even when goextract changes.
        try:
            data = path.read_bytes()
            short_hash = blake2s_short(data)
            size_bytes = len(data)

            try:
                mtime_iso = iso_utc(path.stat().st_mtime)
            except Exception:
                mtime_iso = None

            if isinstance(max_bytes, int) and max_bytes > 0 and size_bytes is not None:
                eligible = size_bytes <= max_bytes

            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("latin-1", errors="replace")

            loc2, sloc2, comment_lines, blank_lines, comment_pct = _count_go_metrics(text)

            loc = loc2
            sloc = goextract_loc_code if goextract_loc_code is not None else sloc2

        except Exception as e:
            parse_status = "error"
            parse_issues.append(f"{rel_file}: {type(e).__name__}")
            try:
                err_snip = repr(e)[:200]
            except Exception:
                err_snip = None

        mod_id = _logical_go_pkg_for_file(
            root=root,
            rel_file_posix=rel_file,
            module_path=module_path,
            dir_to_pkg=dir_to_pkg,
            go_mod_dir=go_mod_dir,
        )
        mod_id = normalize_import_spec(mod_id)

        style_counts = {"stdlib": 0, "module": 0, "local": 0, "relative": 0, "unresolved": 0}
        for spec in sorted(imports_all_set):
            k = classify_go_import(spec, module_path=module_path)
            style_counts[k] = int(style_counts.get(k, 0)) + 1

        name = Path(rel_file).name

        go_language_facts = {
            "package_id": mod_id,
            "file": rel_file,
            "module_path": module_path,
            "go_mod": to_posix(go_mod.relative_to(root)) if go_mod else "",
            "imports": list(tuple(sorted(imports_all_set))),
            "import_classification": dict(style_counts),
        }

        entry = FileEntry(
            id=mod_id,
            file=rel_file,
            name=name,
            parse_status=parse_status,
            imports_all=tuple(sorted(imports_all_set)),
            imports_internal=imports_internal,
            imports_runtime=imports_internal,
            imports_runtime_internal=imports_internal,

            loc=loc,
            sloc=sloc,
            comment_lines=comment_lines,
            blank_lines=blank_lines,
            comment_pct=comment_pct,

            size_bytes=size_bytes,
            mtime=mtime_iso,
            hash=short_hash,
            import_style_counts=style_counts,

            symbol_internal=tuple(),
            imports_external=tuple(),  # computed during internal resolution
            language_facts={
                "go": go_language_facts,
            },

            error_snippet=err_snip,
            eligible=eligible,
        )

        key = f"{mod_id}::{rel_file}"
        files_map[key] = entry

    # ---- Recompute imports_internal via canonical_go.resolve_go_import -------
    internal_pkg_ids: Set[str] = set()
    for fe in files_map.values():
        if fe.id:
            internal_pkg_ids.add(normalize_import_spec(fe.id))

    internal_pkg_ids = {normalize_import_spec(x) for x in internal_pkg_ids if normalize_import_spec(x)}

    files2: Dict[str, FileEntry] = {}
    for k, fe in files_map.items():
        mapped: List[str] = []
        external_specs: List[str] = []

        for spec in (fe.imports_all or ()):
            rr = resolve_go_import(
                spec,
                module_path=module_path,
                internal_pkg_ids=internal_pkg_ids,
                allow_prefix_collapse=True,
            )

            if rr.kind == "internal" and rr.resolved:
                mapped.append(normalize_import_spec(rr.resolved))
            else:
                external_specs.append(normalize_import_spec(spec))

        internal_mods = tuple(sorted(set(mapped)))
        imports_external = tuple(sorted(set(x for x in external_specs if x)))

        unchanged = (
            internal_mods == fe.imports_internal
            and imports_external == fe.imports_external
        )

        if unchanged:
            files2[k] = fe
        else:
            language_facts = dict(getattr(fe, "language_facts", {}) or {})
            go_facts = dict(language_facts.get("go", {}) or {})
            go_facts.update({
                "imports_internal": list(internal_mods),
                "imports_external": list(imports_external),
            })
            language_facts["go"] = go_facts

            files2[k] = FileEntry(
                id=fe.id,
                file=fe.file,
                name=fe.name,
                parse_status=fe.parse_status,
                imports_all=fe.imports_all,
                imports_internal=internal_mods,
                imports_runtime=internal_mods,
                imports_runtime_internal=internal_mods,

                loc=fe.loc,
                sloc=fe.sloc,
                comment_lines=getattr(fe, "comment_lines", None),
                blank_lines=getattr(fe, "blank_lines", None),
                comment_pct=getattr(fe, "comment_pct", None),

                size_bytes=fe.size_bytes,
                mtime=fe.mtime,
                hash=fe.hash,
                import_style_counts=fe.import_style_counts,

                symbol_internal=fe.symbol_internal,
                imports_external=imports_external,
                language_facts=language_facts,

                error_snippet=fe.error_snippet,
                eligible=fe.eligible,
            )

    files_map = files2

    parsed_count = sum(1 for v in files_map.values() if v.parse_status == "ok")
    internal_edge_count = sum(len(v.imports_internal or ()) for v in files_map.values())
    external_import_count = sum(
        len(getattr(v, "imports_external", ()) or ())
        for v in files_map.values()
    )

    total_loc = sum(int(v.loc or 0) for v in files_map.values())
    total_sloc = sum(int(v.sloc or 0) for v in files_map.values())
    total_comment_lines = sum(int(getattr(v, "comment_lines", 0) or 0) for v in files_map.values())
    total_blank_lines = sum(int(getattr(v, "blank_lines", 0) or 0) for v in files_map.values())
    comment_pct_total = (total_comment_lines / total_loc) if total_loc else None
    total_size_bytes = sum(int(v.size_bytes or 0) for v in files_map.values())

    meta: Dict[str, str] = {
        "created": str(iso_utc()),
        "root": str(to_posix(root.resolve())),
        "language": "go",
        "eligible_count": str(len(files_map)),
        "parsed_count": str(parsed_count),
        "internal_edge_count": str(int(internal_edge_count)),
        "external_import_count": str(int(external_import_count)),
        "module_path": str(module_path or ""),
        "go_mod": str(to_posix(go_mod.relative_to(root)) if go_mod else ""),
        "parse_issues_count": str(len(parse_issues)),
        "total_loc": str(int(total_loc)),
        "total_sloc": str(int(total_sloc)),
        "total_comment_lines": str(int(total_comment_lines)),
        "total_blank_lines": str(int(total_blank_lines)),
        "comment_pct": str(comment_pct_total if comment_pct_total is not None else ""),
        "total_size_bytes": str(int(total_size_bytes)),
    }

    idx = FolderIndex(schema=FOLDER_INDEX_SCHEMA, meta=meta, files=files_map)

    log_event(
        "GO_FOLDER_INDEX:build_return",
        root=str(root),
        eligible=len(files_map),
        parsed=parsed_count,
        internal_edges=internal_edge_count,
        parse_issues=len(parse_issues),
        total_loc=total_loc,
        total_sloc=total_sloc,
        total_comment_lines=total_comment_lines,
        total_blank_lines=total_blank_lines,
        comment_pct=comment_pct_total,
    )
    return idx


def save_folder_index(idx: FolderIndex, path: Path) -> None:
    """
    Serialize FolderIndex using the same on-disk convention as Python:
      - record uses 'folder_id' (renamed from 'id')
      - key remains whatever caller used (for Go: "<pkgid>::<file>")
    """
    files_payload: Dict[str, dict] = {}
    for k, v in idx.files.items():
        rec = asdict(v)
        if "id" in rec:
            rec["folder_id"] = rec.pop("id")
        files_payload[k] = rec

    payload = {"schema": idx.schema, "meta": dict(idx.meta), "files": files_payload}
    atomic_write_json(payload, Path(path))


def load_folder_index(path: Path) -> FolderIndex:
    """
    Load FolderIndex strictly using 'folder_id' (same migration posture as Python).
    """
    data = load_json_bytes(Path(path))
    if not isinstance(data, dict) or not data:
        raise ValueError(f"Invalid or empty FolderIndex JSON at {path}")

    schema = data.get("schema")
    if schema != FOLDER_INDEX_SCHEMA:
        raise ValueError(f"Unexpected schema: {schema}")

    files: Dict[str, FileEntry] = {}
    files_raw: Mapping[str, Mapping[str, object]] = data.get("files", {})  # type: ignore[assignment]

    for key, rec in files_raw.items():
        fid = rec.get("folder_id")
        if not fid:
            raise KeyError(f"Missing 'folder_id' in record for {key}")

        imports_all = _tuple_strs(rec.get("imports_all", ()))
        imports_internal = _tuple_strs(rec.get("imports_internal", ()))
        imports_runtime = _tuple_strs(
            rec.get("imports_runtime", imports_internal)
        )
        imports_runtime_internal = _tuple_strs(
            rec.get(
                "imports_runtime_internal",
                rec.get("imports_runtime", imports_internal),
            )
        )
        symbol_internal = _tuple_strs(rec.get("symbol_internal", ()))
        imports_external = _tuple_strs(rec.get("imports_external", ()))
        language_facts = _mapping_or_empty(rec.get("language_facts", {}))

        files[key] = FileEntry(
            id=str(fid),
            file=str(rec.get("file", "")),
            name=str(rec.get("name", "")),
            parse_status=str(rec.get("parse_status", "ok")),
            imports_all=imports_all,
            imports_internal=imports_internal,
            imports_runtime=imports_runtime,
            imports_runtime_internal=imports_runtime_internal,
            loc=rec.get("loc"),
            sloc=rec.get("sloc"),
            comment_lines=rec.get("comment_lines"),
            blank_lines=rec.get("blank_lines"),
            comment_pct=rec.get("comment_pct"),
            size_bytes=rec.get("size_bytes"),
            mtime=rec.get("mtime"),
            hash=rec.get("hash"),
            import_style_counts=_mapping_or_empty(
                rec.get("import_style_counts", {})
            ),
            symbol_internal=symbol_internal,
            imports_external=imports_external,
            language_facts=language_facts,
            error_snippet=rec.get("error_snippet"),
            eligible=bool(rec.get("eligible", True)),
        )

    return FolderIndex(schema=data["schema"], meta=data.get("meta", {}), files=files)