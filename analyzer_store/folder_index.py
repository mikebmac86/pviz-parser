from __future__ import annotations

"""
FolderIndex — deterministic snapshot of a workspace's Python files.

This module consumes the existing analyzer primitives (fs, parse, imports_lex,
module_resolve, module_map) and emits a JSON-ready structure conforming to the
schema "folder-index-1.0.0" defined in analyzer_store.types.

Public API
----------
build_folder_index(root: Path, cfg) -> FolderIndex
save_folder_index(idx: FolderIndex, path: Path) -> None
load_folder_index(path: Path) -> FolderIndex

Notes
-----
• Deterministic: stable sorting, deduped lists, positional-agnostic IDs.
• Internal edges use ModuleMap to decide if a target module belongs to the folder.
• Error tolerant: parse_status captures failures; facts may be partial.
• Atomic writes via analyzer_store.io_utils.atomic_write_json.
• IMPORTANT: The per-file key and FileEntry.id are **module ids relative to the
  chosen scan root**. If you choose a *sub-package* root (e.g. 'scrapy/core'),
  modules will be named 'downloader', 'scraper', ... rather than
  'scrapy.core.downloader'. Absolute imports like 'scrapy.core.*' will then be
  treated as external, which is why scanning deeply under 'scrapy/core' yields
  few or no internal edges. For full graph connectivity, prefer scanning from
  the package root ('scrapy') or repo root.

"""

from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Set, Tuple
import ast
import os  # local default_store_root helper uses env/home

from analyzer_store.types import (
    FOLDER_INDEX_SCHEMA,
    FileEntry,
    FolderIndex,
)
from analyzer_store.io_utils import iso_utc, blake2s_short, atomic_write_json, load_json_bytes
from concurrent.futures import ProcessPoolExecutor, as_completed
import json
# Analyzer primitives (existing project modules)
from analyzer import fs as a_fs
from analyzer.fs import ensure_dir_root, normalize
from analyzer import parse as a_parse
from analyzer import imports_lex as a_lex
from analyzer import module_resolve as a_resolve
from analyzer import config as a_config
from adapters.canonical import moduleish_for_path, repo_rel, to_posix
from core.store_root import default_store_root
# Diagnostics logging (central controller)
try:
    from diagnostics.logging import log_event
except Exception:  # pragma: no cover
    def log_event(*_a, **_k):
        return

def _docstring_line_numbers(text: str) -> Set[int]:
    """
    Return physical line numbers occupied by real Python docstring statements.

    This counts module, class, function, and async-function docstrings using AST
    source positions. It does not count arbitrary string literals as comments.
    """
    try:
        tree = ast.parse(text)
    except Exception:
        return set()

    doc_lines: Set[int] = set()

    for node in ast.walk(tree):
        if not isinstance(
            node,
            (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            continue

        body = getattr(node, "body", None) or []
        if not body:
            continue

        first = body[0]
        if not isinstance(first, ast.Expr):
            continue

        value = getattr(first, "value", None)
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            continue

        start = getattr(first, "lineno", None)
        end = getattr(first, "end_lineno", start)

        if start is None:
            continue

        for ln in range(start, (end or start) + 1):
            doc_lines.add(ln)

    return doc_lines


def count_python_metrics(text: str):
    """
    Count basic Python file metrics.

    Definitions:
      loc:
        Physical line count, including blank lines.

      sloc:
        Nonblank, non-comment, non-docstring code lines.

      comment_lines:
        Physical lines that are either:
          - # comment-only lines, or
          - real module/class/function/async-function docstring lines.

        This intentionally treats docstrings as comment/documentation lines.

      blank_lines:
        Physical blank/whitespace-only lines.

      comment_pct:
        comment_lines / physical loc.
    """
    loc = 0
    sloc = 0
    comment_lines = 0
    blank_lines = 0

    doc_lines = _docstring_line_numbers(text)

    for lineno, raw in enumerate(text.splitlines(), start=1):
        loc += 1
        line = raw.strip()

        if not line:
            blank_lines += 1
            continue

        if lineno in doc_lines:
            comment_lines += 1
            continue

        if line.startswith("#"):
            comment_lines += 1
            continue

        sloc += 1

    comment_pct = (comment_lines / loc) if loc else None
    return loc, sloc, comment_lines, blank_lines, comment_pct

# ---------------------------------------------------------------------------
# TC line-range collector (used by _expand_imports_ast runtime_only mode)
# ---------------------------------------------------------------------------

def _tc_line_ranges(tree: ast.AST) -> Set[int]:
    """
    Return the set of line numbers that fall inside any TYPE_CHECKING block.

    Uses collect_top_level_imports_and_tc_blocks from ast_common when available;
    falls back to a direct AST walk that identifies the canonical pattern:

        if TYPE_CHECKING:
            ...

    The fallback covers aliased forms like `if typing.TYPE_CHECKING:` as well.
    """
    tc_lines: Set[int] = set()

    # Preferred: reuse the project's own TC-block extractor which already
    # handles aliases (e.g. `TC = TYPE_CHECKING`).
    try:
        from analyzer.ast_common import (
            collect_typing_aliases_and_tc_names,
            collect_top_level_imports_and_tc_blocks,
        )
        typing_aliases, tc_names = collect_typing_aliases_and_tc_names(tree)
        _, tc_blocks = collect_top_level_imports_and_tc_blocks(
            tree, typing_aliases, tc_names
        )
        for block in tc_blocks:
            for stmt in block:
                start = getattr(stmt, "lineno", None)
                end = getattr(stmt, "end_lineno", start)
                if start is not None:
                    for ln in range(start, (end or start) + 1):
                        tc_lines.add(ln)
        return tc_lines
    except Exception:
        pass

    # Fallback: walk for `if TYPE_CHECKING:` / `if typing.TYPE_CHECKING:` patterns
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        is_tc = False
        if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
            is_tc = True
        elif (
            isinstance(test, ast.Attribute)
            and test.attr == "TYPE_CHECKING"
        ):
            is_tc = True
        if not is_tc:
            continue
        for child in ast.walk(node):
            start = getattr(child, "lineno", None)
            end = getattr(child, "end_lineno", start)
            if start is not None:
                for ln in range(start, (end or start) + 1):
                    tc_lines.add(ln)

    return tc_lines

def _build_folder_index_batch(
    root: Path,
    paths: List[Path],
    cfg: "a_config.AnalyzerCfg",
) -> Tuple[List["FileEntry"], List[str]]:
    """
    Worker: process a batch of paths and return FileEntry list + parse_issues.

    NOTE: We intentionally *do not* use ModuleMap or log here — this is
    pure CPU work with no prints, to keep multiprocessing clean. The
    final internal-imports pass in the parent recomputes imports_internal
    and imports_runtime_internal.

    v1.1: Splits import collection into imports_all (all imports including
    TYPE_CHECKING-gated ones) and imports_runtime (runtime-only, no TC).
    Both are stored on FileEntry; imports_runtime_internal is resolved in
    the parent recompute pass once all internal module IDs are known.
    """
    files: List[FileEntry] = []
    parse_issues: List[str] = []

    root = normalize(ensure_dir_root(Path(root)))

    for path in paths:
        # Consumer module logical name (ID space for FolderIndex & NodeFacts)
        mod_id = _logical_module_for_path(root, path)

        # Defaults
        parse_status = "ok"
        imports_all: Set[str] = set()
        imports_runtime: Set[str] = set()                    # runtime-only, no TC
        imports_internal: Tuple[str, ...] = tuple()          # recomputed later
        imports_runtime_internal: Tuple[str, ...] = tuple()  # recomputed later

        loc: Optional[int] = None
        sloc: Optional[int] = None
        comment_lines: Optional[int] = None
        blank_lines: Optional[int] = None
        comment_pct: Optional[float] = None

        size_bytes: Optional[int] = None
        mtime_iso: Optional[str] = None
        short_hash: Optional[int] = None
        err_snip: Optional[str] = None
        eligible = True

        style_counts = {
            "absolute": 0,
            "relative": 0,
            "from": 0,
            "star": 0,
            "dynamic": None,
        }

        try:
            # Read file once (binary) for hashing; pass decoded text to parser
            data = path.read_bytes()
            short_hash = blake2s_short(data)
            size_bytes = len(data)
            mtime_iso = iso_utc(path.stat().st_mtime)

            max_bytes = getattr(cfg, "max_file_bytes", None)
            if isinstance(max_bytes, int) and max_bytes > 0 and size_bytes is not None:
                eligible = size_bytes <= max_bytes

            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("latin-1", errors="replace")

            loc, sloc, comment_lines, blank_lines, comment_pct = count_python_metrics(text)

            # Parse with project rules (this also guards huge files via cfg)
            try:
                parsed = a_parse.parse_file(path, cfg)
            except Exception as _e:
                parsed = None
                parse_status = "error"
                parse_issues.append(f"{repo_rel(path, root)}: {type(_e).__name__}")

            # Lex imports from text (prefer parsed side-channels if present)
            try:
                refs: List[a_lex.ImportRef] = list(
                    a_lex.extract_imports(text, cfg, module_name=mod_id)  # type: ignore[attr-defined]
                )
            except Exception:
                refs = []

            # Expand lexical refs into logical module targets.
            # Split into imports_all (everything) and imports_runtime (no TC).
            for ref in refs:
                try:
                    names = getattr(ref, "names", None) or []
                    is_star = getattr(ref, "is_star", False)
                    mod_tok = getattr(ref, "module_token", None)
                    is_tc = bool(getattr(ref, "under_type_checking", False))

                    # Normalize relative imports to absolute module paths
                    if isinstance(mod_tok, str) and mod_tok.startswith("."):
                        try:
                            mod_tok = a_resolve.absolutize_module(mod_tok, mod_id or "")
                        except Exception:
                            pass

                    expanded = a_resolve.expand_targets(mod_tok, names, is_star, mod_id)

                    # stats
                    if getattr(ref, "is_relative", False):
                        style_counts["relative"] += 1
                    elif getattr(ref, "is_from", False):
                        style_counts["from"] += 1
                    else:
                        style_counts["absolute"] += 1

                    if is_star:
                        style_counts["star"] += 1

                    for tgt in expanded or []:
                        if not tgt:
                            continue

                        tgt_norm = str(tgt).strip().strip(".")
                        if not tgt_norm:
                            continue

                        imports_all.add(tgt_norm)

                        if not is_tc:
                            imports_runtime.add(tgt_norm)

                except Exception:
                    # Keep robust; a single bad ref shouldn't poison the file
                    continue

            # Always augment with AST-derived imports (fills missing relative imports).
            # Full set goes into imports_all; runtime-only set into imports_runtime.
            ast_targets_all = _expand_imports_ast(text, mod_id)
            ast_targets_runtime = _expand_imports_ast(text, mod_id, runtime_only=True)

            for tgt in ast_targets_all:
                imports_all.add(tgt)

            for tgt in ast_targets_runtime:
                imports_runtime.add(tgt)

        except Exception as exc:
            parse_status = "error"

            try:
                err_snip = repr(exc)[:200]
            except Exception:
                err_snip = None

            try:
                parse_issues.append(f"{repo_rel(path, root)}: {type(exc).__name__}")
            except Exception:
                parse_issues.append(f"{path}: {type(exc).__name__}")

        name = mod_id.split(".")[-1] if mod_id else ""
        entry = FileEntry(
            id=mod_id,
            file=repo_rel(path, root),
            name=name,
            parse_status=parse_status,
            imports_all=tuple(sorted(imports_all)),
            imports_internal=imports_internal,
            imports_runtime=tuple(sorted(imports_runtime)),
            imports_runtime_internal=imports_runtime_internal,
            loc=loc,
            sloc=sloc,
            comment_lines=comment_lines,
            blank_lines=blank_lines,
            comment_pct=comment_pct,
            size_bytes=size_bytes,
            mtime=mtime_iso,
            hash=short_hash,
            import_style_counts=style_counts,
            error_snippet=err_snip,
            eligible=eligible,
        )

        files.append(entry)

    return files, parse_issues

# ---------------------------------------------------------------------------
# Helpers: imports + file enumeration
# ---------------------------------------------------------------------------

def _expand_imports_ast(
    text: str,
    logical_mod: str | None,
    *,
    runtime_only: bool = False, 
) -> list[str]:
    """
    Fallback extractor using Python AST; returns module targets like 'pkg.mod'.

    v1.1: When runtime_only=True, nodes whose line numbers fall inside any
    TYPE_CHECKING block are skipped, producing a runtime-only import list.
    """
    out: list[str] = []
    try:
        tree = ast.parse(text)
    except Exception:
        return out

    tc_lines: Set[int] = _tc_line_ranges(tree) if runtime_only else set()

    base_parts = (logical_mod or "").split(".") if logical_mod else []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue

        if runtime_only:
            node_line = getattr(node, "lineno", None)
            if node_line is not None and node_line in tc_lines:
                continue

        if isinstance(node, ast.Import):
            for n in node.names:
                if n.name:
                    out.append(n.name)

        elif isinstance(node, ast.ImportFrom):
            level = getattr(node, "level", 0) or 0
            mod = node.module or ""

            # Resolve relative: drop 'level' parts from end of logical module
            if level > 0:
                pre = base_parts[:-level] if level <= len(base_parts) else []
                base = ".".join([p for p in pre if p])
                mod_abs = ".".join([base, mod]).strip(".")
            else:
                mod_abs = mod

            if not node.names:
                # 'from pkg import *'
                if mod_abs:
                    out.append(mod_abs)
            else:
                for n in node.names:
                    nm = getattr(n, "name", "")
                    if mod_abs and nm:
                        out.append(f"{mod_abs}.{nm}")
                    elif mod_abs:
                        out.append(mod_abs)
                    elif nm:
                        out.append(nm)

    # de-dup while preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for m in out:
        if m and m not in seen:
            seen.add(m)
            uniq.append(m)
    return uniq


def _iter_python_files(root: Path, cfg: a_config.AnalyzerCfg) -> List[Path]:
    """
    Unified iterable of Python files:
      - everything the project walker yields
      - PLUS any root-level *.py files (like 'main.py') that some walkers skip.

    De-duped and sorted (POSIX) for determinism.
    """
    root = root.resolve()
    seen: Set[Path] = set()

    # 1) Files from the project's configured iterator
    try:
        for p in a_fs.iter_project_files(root, cfg):
            rp = Path(p).resolve()
            if rp.suffix.lower() == ".py" and rp.exists() and rp not in seen:
                seen.add(rp)
    except Exception:
        pass

    # 2) Explicitly add root-level *.py files
    try:
        for p in root.glob("*.py"):
            rp = p.resolve()
            if rp.suffix.lower() == ".py" and rp.exists() and rp not in seen:
                seen.add(rp)
    except Exception:
        pass

    return sorted(seen, key=lambda p: to_posix(str(p)))


def _logical_module_for_path(root: Path, path: Path) -> str:
    """
    Derive a *module id* for a file under the given scan root.

    Behavior is intentionally **relative to the chosen root**:

      root = '/project/scrapy/core'
      path = '/project/scrapy/core/downloader/__init__.py'
      → 'downloader'

    i.e. we do **not** invent a 'scrapy.core.' prefix. This is why choosing a
    sub-package root like 'scrapy/core' yields ids 'downloader', 'scraper', ...

    If resolver fails, we derive from repo-relative POSIX path:
      rel = 'downloader/__init__.py' → 'downloader'
    """
    try:
        _phys, logical_mod = a_resolve.module_names_for_path(root, path)
    except Exception:
        logical_mod = None

    if logical_mod:
        return logical_mod

    try:
        rel = to_posix(path.relative_to(root))
    except Exception:
        rel = to_posix(path)
    return moduleish_for_path(rel)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def build_folder_index(root: Path, cfg: a_config.AnalyzerCfg) -> FolderIndex:
    # Entry + root normalization
    log_event("FOLDER_INDEX:build_enter", root=str(root))

    # Treat root strictly as a read-only scan anchor
    root = normalize(ensure_dir_root(Path(root)))
    log_event("FOLDER_INDEX:root_normalized", root=str(root))

    # Deterministic, de-duped list of project files (includes root/*.py)
    log_event("FOLDER_INDEX:iter_files_begin", root=str(root))
    paths: List[Path] = _iter_python_files(root, cfg)
    log_event("FOLDER_INDEX:iter_files_end", root=str(root), count=len(paths))

    files: Dict[str, FileEntry] = {}
    parse_issues: List[str] = []

    if not paths:
        log_event("FOLDER_INDEX:no_paths_short_circuit", root=str(root))
    else:
        # ---------------- Multiprocessing: batch paths across workers --------
        cpu_count = os.cpu_count() or 1
        max_workers = min(cpu_count, len(paths))

        if max_workers <= 1:
            # Fallback: sequential
            log_event("FOLDER_INDEX:sequential_fallback_begin", root=str(root), paths=len(paths))
            batch_files, batch_issues = _build_folder_index_batch(root, paths, cfg)
            for fe in batch_files:
                files[fe.id] = fe
            parse_issues.extend(batch_issues)
            log_event(
                "FOLDER_INDEX:sequential_fallback_end",
                root=str(root),
                batch_size=len(batch_files),
                parse_issues=len(batch_issues),
            )
        else:
            def _chunk(seq: List[Path], n: int) -> List[List[Path]]:
                chunks: List[List[Path]] = []
                base = len(seq) // n
                extra = len(seq) % n
                start = 0
                for i in range(n):
                    size = base + (1 if i < extra else 0)
                    if size <= 0:
                        continue
                    end = start + size
                    chunks.append(seq[start:end])
                    start = end
                return chunks

            batches = _chunk(paths, max_workers)
            log_event(
                "FOLDER_INDEX:multiprocessing_begin",
                root=str(root),
                workers=max_workers,
                batches=len(batches),
            )

            with ProcessPoolExecutor(max_workers=max_workers) as ex:
                futures = {
                    ex.submit(_build_folder_index_batch, root, batch, cfg): idx
                    for idx, batch in enumerate(batches)
                }
                for fut in as_completed(futures):
                    idx = futures[fut]
                    batch_files, batch_issues = fut.result()
                    for fe in batch_files:
                        files[fe.id] = fe
                    parse_issues.extend(batch_issues)
                    log_event(
                        "FOLDER_INDEX:worker_done",
                        root=str(root),
                        batch_index=idx,
                        batch_size=len(batch_files),
                        parse_issues=len(batch_issues),
                    )

            log_event(
                "FOLDER_INDEX:multiprocessing_end",
                root=str(root),
                total_files=len(files),
                parse_issues=len(parse_issues),
            )

    # ---------- Normalize IDs to src-root key-space (monorepo-safe) ----------
    log_event("FOLDER_INDEX:normalize_ids_begin", root=str(root))

    cand_names = ("backend", "src", "server", "app", "services", "python")
    src_root_paths: List[Path] = []
    for name in cand_names:
        p = root / name
        try:
            if p.is_dir():
                it = p.rglob("*.py")
                if next(it, None) is not None:
                    src_root_paths.append(p)
        except Exception:
            continue

    # repo root fallback last
    id_roots: List[Path] = src_root_paths[:] + [root]
    id_roots.sort(key=lambda p: (-len(p.parts), str(p)))

    def _choose_id_root_for_rel(rel_posix: str) -> Path:
        ap = root / rel_posix
        try:
            ap = ap.resolve()
        except Exception:
            pass
        for r in id_roots:
            try:
                ap.relative_to(r.resolve())
                return r
            except Exception:
                continue
        return root

    files2: Dict[str, FileEntry] = {}
    collisions: List[str] = []

    for old_id, fe in list(files.items()):
        rel_file = fe.file
        new_id = old_id
        if isinstance(rel_file, str) and rel_file:
            id_root = _choose_id_root_for_rel(rel_file)
            new_id = _logical_module_for_path(id_root, root / rel_file)

        if new_id in files2:
            collisions.append(f"{new_id} <= {old_id} AND {files2[new_id].id}")
            continue

        if new_id == fe.id:
            files2[new_id] = fe
        else:
            files2[new_id] = FileEntry(
                id=new_id,
                file=fe.file,
                name=fe.name,
                parse_status=fe.parse_status,
                imports_all=fe.imports_all,
                imports_internal=fe.imports_internal,
                imports_runtime=fe.imports_runtime,              
                imports_runtime_internal=fe.imports_runtime_internal,  
                loc=fe.loc,
                sloc=fe.sloc,
                comment_lines=fe.comment_lines,
                blank_lines=fe.blank_lines,
                comment_pct=fe.comment_pct,
                size_bytes=fe.size_bytes,
                mtime=fe.mtime,
                hash=fe.hash,
                import_style_counts=fe.import_style_counts,
                error_snippet=fe.error_snippet,
                eligible=fe.eligible,
            )

    files = files2

    log_event(
        "FOLDER_INDEX:normalize_ids_end",
        root=str(root),
        id_roots=[to_posix(p.relative_to(root)) if p != root else "" for p in id_roots],
        collisions=len(collisions),
    )
    if collisions:
        log_event("FOLDER_INDEX:normalize_ids_collisions", sample=collisions[:10])

    # ---------- Recompute internal imports in normalized id-space ----------
    # v1.1: Also computes imports_runtime_internal from imports_runtime.
    log_event("FOLDER_INDEX:recompute_internal_imports_begin", root=str(root))
    internal_ids = set(files.keys())
    closest_cache: Dict[str, Optional[str]] = {}

    def _closest_internal(mod: str) -> str | None:
        if mod in closest_cache:
            return closest_cache[mod]
        if mod in internal_ids:
            closest_cache[mod] = mod
            return mod
        parts = mod.split(".")
        while len(parts) > 1:
            parts.pop()
            cand = ".".join(parts)
            if cand in internal_ids:
                closest_cache[mod] = cand
                return cand
        closest_cache[mod] = None
        return None

    files3: Dict[str, FileEntry] = {}
    for mod_id, fe in list(files.items()):
        # Existing: all imports → internal
        all_mods = list(fe.imports_all or ())
        mapped: List[str] = []
        for m in all_mods:
            ci = _closest_internal(m)
            if ci is not None:
                mapped.append(ci)
        internal_mods = tuple(sorted(set(mapped)))
        runtime_mods_raw = list(fe.imports_runtime or ())
        runtime_mapped: List[str] = []
        for m in runtime_mods_raw:
            ci = _closest_internal(m)
            if ci is not None:
                runtime_mapped.append(ci)
        runtime_internal_mods = tuple(sorted(set(runtime_mapped)))

        # Only reconstruct if something changed
        unchanged = (
            internal_mods == fe.imports_internal
            and runtime_internal_mods == fe.imports_runtime_internal
        )
        if unchanged:
            files3[mod_id] = fe
        else:
            files3[mod_id] = FileEntry(
                id=fe.id,
                file=fe.file,
                name=fe.name,
                parse_status=fe.parse_status,
                imports_all=fe.imports_all,
                imports_internal=internal_mods,
                imports_runtime=fe.imports_runtime,  
                imports_runtime_internal=runtime_internal_mods,  
                loc=fe.loc,
                sloc=fe.sloc,
                comment_lines=fe.comment_lines,
                blank_lines=fe.blank_lines,
                comment_pct=fe.comment_pct,
                size_bytes=fe.size_bytes,
                mtime=fe.mtime,
                hash=fe.hash,
                import_style_counts=fe.import_style_counts,
                error_snippet=fe.error_snippet,
                eligible=fe.eligible,
            )

    files = files3
    log_event("FOLDER_INDEX:recompute_internal_imports_end", root=str(root))

    # ---- Meta summary (what integration expects) -------------------------
    log_event("FOLDER_INDEX:meta_summary_begin", root=str(root))

    parsed_files = [v.file for v in files.values()]  # POSIX, repo-relative
    parsed_count = sum(1 for v in files.values() if v.parse_status == "ok")
    internal_edge_count = sum(len(v.imports_internal) for v in files.values())
    runtime_internal_edge_count = sum(len(v.imports_runtime_internal) for v in files.values())

    # FolderIndex.meta is typed Mapping[str, str]; stringify values to be safe
    meta: Dict[str, str] = {
        "created": str(iso_utc()),
        "root": str(to_posix(root.resolve())),
        "eligible_count": str(len(files)),
        "parsed_count": str(parsed_count),
        "internal_edge_count": str(int(internal_edge_count)),
        "runtime_internal_edge_count": str(int(runtime_internal_edge_count)),
    }
    # Keep additional meta as JSON-ish strings if you want them available,
    # but remain type-correct.
    meta["parse_issues_count"] = str(len(parse_issues))
    meta["external_roots"] = json.dumps(
        [to_posix(p.relative_to(root)) if p != root else "" for p in id_roots],
        ensure_ascii=False,
    )

    idx = FolderIndex(
        schema=FOLDER_INDEX_SCHEMA,
        meta=meta,
        files=files,
    )

    log_event(
        "FOLDER_INDEX:meta_summary_end",
        root=str(root),
        eligible=len(files),
        parsed=parsed_count,
        internal_edges=internal_edge_count,
        runtime_internal_edges=runtime_internal_edge_count,
        parse_issues=len(parse_issues),
    )
    log_event("FOLDER_INDEX:build_return", root=str(root))
    return idx

def save_folder_index(idx: FolderIndex, path: Path) -> None:
    """
    Serialize the FolderIndex using the new folder_id schema only.
    Drops legacy 'id' key to eliminate ambiguity.

    v1.1: Serializes imports_runtime and imports_runtime_internal.
    """
    files_payload: Dict[str, dict] = {}
    for k, v in idx.files.items():
        rec = asdict(v)
        # Rename 'id' → 'folder_id' for clarity
        if "id" in rec:
            rec["folder_id"] = rec.pop("id")
        files_payload[k] = rec

    payload = {
        "schema": idx.schema,
        "meta": dict(idx.meta),
        "files": files_payload,
    }
    atomic_write_json(payload, Path(path))

def load_folder_index(path: Path) -> FolderIndex:
    """
    Load FolderIndex strictly using folder_id.
    Fails fast if legacy 'id' keys are still present to ensure clean migration.
    (Uses tolerant bytes loader: orjson fast-path, UTF-8 fallback.)

    v1.1: Loads imports_runtime and imports_runtime_internal with graceful
    defaults (empty tuple) for backward compatibility with v1.0 indexes.
    """
    data = load_json_bytes(Path(path))
    if not isinstance(data, dict) or not data:
        raise ValueError(f"Invalid or empty FolderIndex JSON at {path}")

    schema = data.get("schema")

    # ------------------------------------------------------------------
    # Buckets-merged placeholder guard
    #
    # When the Python bucket fails, the merger writes a placeholder file like:
    #   { "folders": [], "files": [], "meta": {...} }
    # which is NOT a real FolderIndex artifact and has no schema.
    # ------------------------------------------------------------------
    if schema is None:
        files_val = data.get("files", None)
        folders_val = data.get("folders", None)
        meta_val = data.get("meta", None)

        is_placeholder = (
            isinstance(meta_val, dict)
            and isinstance(files_val, list)   # placeholder uses list, not mapping
            and isinstance(folders_val, list) # placeholder uses list, not mapping
        )
        if is_placeholder:
            raise ValueError(
                f"FolderIndex at {path} is a buckets-merged placeholder (no schema). "
                f"This usually means the Python analyzer failed earlier. "
                f"Delete {path.name} and rerun, or ensure bucket analyzers write "
                f"analyzer_store-native artifacts before load."
            )

    if schema != FOLDER_INDEX_SCHEMA:
        raise ValueError(f"Unexpected schema: {schema}")

    files: Dict[str, FileEntry] = {}

    # Real FolderIndex uses a mapping: { "<mod_id>": { ... } }
    files_raw: Mapping[str, Mapping[str, object]] = data.get("files", {})  # type: ignore[assignment]

    for mod_id, rec in files_raw.items():
        fid = rec.get("folder_id")
        if not fid:
            raise KeyError(f"Missing 'folder_id' in record for {mod_id}")

        files[mod_id] = FileEntry(
            id=str(fid),
            file=str(rec["file"]),
            name=str(rec["name"]),
            parse_status=str(rec.get("parse_status", "ok")),
            imports_all=tuple(rec.get("imports_all", []) or ()),
            imports_internal=tuple(rec.get("imports_internal", []) or ()),
            imports_runtime=tuple(rec.get("imports_runtime", []) or ()),  
            imports_runtime_internal=tuple(rec.get("imports_runtime_internal", []) or ()),
            loc=rec.get("loc"),
            sloc=rec.get("sloc"),
            comment_lines=rec.get("comment_lines"),
            blank_lines=rec.get("blank_lines"),
            comment_pct=rec.get("comment_pct"),
            size_bytes=rec.get("size_bytes"),
            mtime=rec.get("mtime"),
            hash=rec.get("hash"),
            import_style_counts=rec.get("import_style_counts", {}),
            error_snippet=rec.get("error_snippet"),
            eligible=rec.get("eligible", True),
        )

    return FolderIndex(schema=data["schema"], meta=data.get("meta", {}), files=files)

