# analyzer/ts/run.py
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Sequence
import time

from .config import TSAnalyzerCfg
from .discover import discover_files
from .extract_imports import extract_imports_and_crosstalk
from .model import RawImport, ResolvedImport
from .parser_runtime import TSRuntime
from .resolve_imports import resolve_specifier_to_file
from .build_artifacts import FileFacts, build_edges, build_folder_index, build_nodefacts
from .write_artifacts import write_json
from .symbols_js import extract_js_symbols

from .canonical_web import canon_web_file_id, is_repo_relative, strip_query_and_hash
from .tsconfig_paths import (
    TSPathMap,
    load_effective_repo_tsconfig_paths,
    get_pathmap_for_file,
)

try:
    from diagnostics.logging import log_event as _log_event  # type: ignore
except Exception:  # pragma: no cover
    _log_event = None  # type: ignore


def log_event(name: str, **k) -> None:
    if _log_event is not None:
        try:
            _log_event(name, **k)
            return
        except Exception:
            pass

    parts = " ".join(f"{kk}={k[kk]!r}" for kk in sorted(k.keys()))
    print(f"[TS] {name} {parts}".rstrip())


def _count_ts_js_metrics(text: str) -> Dict[str, object]:
    """
    Approximate line metrics for TypeScript / JavaScript.

    Definitions
    -----------
    loc:
        Physical line count, including blank lines.

    sloc:
        Lines containing executable/code text after stripping comments.

    comment_lines:
        Comment-only physical lines. Inline comments on code lines are not
        counted as separate comment lines.

    blank_lines:
        Physical blank/whitespace-only lines.

    comment_pct:
        comment_lines / loc.

    Notes
    -----
    This is intentionally a lightweight scanner, not a full JS/TS lexer. It
    handles // comments and /* ... */ block comments, including multi-line
    block comments. It may misclassify comment markers inside string or regex
    literals, but is stable and fast for large repository metrics.
    """
    loc = 0
    sloc = 0
    comment_lines = 0
    blank_lines = 0
    in_block = False

    for raw in text.splitlines():
        loc += 1

        if not raw.strip():
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

            idx_line = raw.find("//", i)
            idx_block = raw.find("/*", i)

            next_idx = None
            kind = None

            if idx_line != -1 and (idx_block == -1 or idx_line < idx_block):
                next_idx = idx_line
                kind = "line"
            elif idx_block != -1:
                next_idx = idx_block
                kind = "block"

            if next_idx is None:
                code_chars.append(raw[i:])
                break

            if next_idx > i:
                code_chars.append(raw[i:next_idx])

            saw_comment = True

            if kind == "line":
                i = n
                break

            in_block = True
            i = next_idx + 2

        has_code = bool("".join(code_chars).strip())

        if has_code:
            sloc += 1
        elif saw_comment:
            comment_lines += 1

    comment_pct = (comment_lines / loc) if loc else None

    return {
        "loc": loc,
        "sloc": sloc,
        "comment_lines": comment_lines,
        "blank_lines": blank_lines,
        "comment_pct": comment_pct,
    }


def _filefacts_kwargs_with_metrics(
    *,
    rel_posix: str,
    loc: int,
    sloc: int,
    comment_lines: int,
    blank_lines: int,
    comment_pct: Optional[float],
    parse_status: str,
    imports: List[str],
    exports: List[str],
    functions: List[str],
    classes: List[str],
    globals: List[str],
    facts: Dict[str, object],
    crosstalk_candidates_ts_v1: List[object],
) -> Dict[str, object]:
    """
    Build FileFacts kwargs including the normalized metrics.

    Important:
      imports must remain the raw syntax import specs extracted from
      extract_imports(). Do not replace this list with resolved targets.
      build_artifacts.py receives resolved_by_src separately and derives:
        - imports
        - imports_all_raw
        - imports_unresolved
    """
    return {
        "rel_posix": rel_posix,
        "loc": loc,
        "sloc": sloc,
        "comment_lines": comment_lines,
        "blank_lines": blank_lines,
        "comment_pct": comment_pct,
        "parse_status": parse_status,
        "imports": imports,
        "exports": exports,
        "functions": functions,
        "classes": classes,
        "globals": globals,
        "facts": facts,
        "crosstalk_candidates_ts_v1": crosstalk_candidates_ts_v1,
    }


def _same_pathmap(a: TSPathMap, b: TSPathMap) -> bool:
    """
    Compare effective TS path maps.

    TSPathMap no longer exposes raw base_url. Use paths_base_dir and
    uses_legacy_base_url instead.
    """
    return (
        a.tsconfig_rel_dir == b.tsconfig_rel_dir
        and a.paths_base_dir == b.paths_base_dir
        and a.paths == b.paths
        and a.uses_legacy_base_url == b.uses_legacy_base_url
    )


def _safe_resolve_spec(
    *,
    repo_root: Path,
    src: str,
    spec: str,
    cfg: TSAnalyzerCfg,
    file_pathmaps: Sequence[TSPathMap] | None,
) -> str | None:
    """
    Safe wrapper around resolve_specifier_to_file().

    Resolution should never crash artifact generation because of pathological
    import strings. This is defensive only; import extraction should still stay
    syntax-strict upstream.
    """
    spec_clean = strip_query_and_hash((spec or "").strip())
    if not spec_clean:
        return None

    # Do not probe absurd literal strings as paths.
    if len(spec_clean.encode("utf-8", errors="ignore")) > 512:
        return None

    # Skip schemes / virtual package prefixes that should not hit disk.
    if spec_clean.startswith((
        "data:",
        "http:",
        "https:",
        "node:",
        "file:",
        "npm:",
        "bun:",
        "deno:",
        "virtual:",
        "#",
    )):
        return None

    try:
        return resolve_specifier_to_file(
            repo_root=repo_root,
            src_rel_posix=src,
            spec=spec_clean,
            treat_slash_root_as_repo_root=cfg.treat_slash_root_as_repo_root,
            pathmaps=file_pathmaps if file_pathmaps else None,
            pathmap=None,
        )
    except OSError as e:
        # Common on pathological path probes: filename too long.
        if getattr(e, "errno", None) == 36:
            return None
        raise


def _summarize_resolved_imports(
    *,
    file_facts: List[FileFacts],
    resolved_by_src: Dict[str, List[ResolvedImport]],
    unresolved_after: int,
) -> None:
    """
    Non-mutating replacement for the old imports_updated_in_facts block.

    The old behavior overwrote FileFacts.imports with a mixed list of:
      - resolved dst_node_id
      - unresolved original spec

    That polluted imports_all_raw downstream. This function only logs summary
    counts and leaves ff.imports untouched.
    """
    files_with_resolved_imports = 0
    total_resolved_imports = 0

    for ff in file_facts:
        resolved_imports = resolved_by_src.get(ff.rel_posix, [])
        resolved_count = sum(1 for ri in resolved_imports if ri.dst_node_id)
        if resolved_count:
            files_with_resolved_imports += 1
            total_resolved_imports += resolved_count

    log_event(
        "TS:imports_resolved_summary",
        files_with_resolved_imports=files_with_resolved_imports,
        total_imports_resolved=total_resolved_imports,
        total_imports_unresolved=unresolved_after,
    )


def analyze_repo(*, repo_root: Path, artifact_dir: Path, cfg: TSAnalyzerCfg) -> None:
    t_all = time.perf_counter()

    repo_root = repo_root.resolve()
    artifact_dir = artifact_dir.resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)

    log_event("TS:analyze_repo_begin", repo_root=str(repo_root), artifact_dir=str(artifact_dir))

    t0 = time.perf_counter()
    pm_root = load_effective_repo_tsconfig_paths(repo_root)
    log_event("TS:tsconfig_root_loaded", ok=(pm_root is not None), ms=int((time.perf_counter() - t0) * 1000))

    discovered = discover_files(
        repo_root=repo_root,
        include_globs=cfg.include_globs,
        exclude_globs=cfg.exclude_globs,
    )
    log_event("TS:discover_done", files=len(discovered))

    if cfg.skip_d_ts:
        before = len(discovered)
        discovered = [d for d in discovered if not d.rel_posix.endswith(".d.ts")]
        log_event("TS:skip_d_ts_applied", before=before, after=len(discovered))

    runtime = TSRuntime()

    file_facts: List[FileFacts] = []
    raw_imports_by_file: Dict[str, List[RawImport]] = {}

    total_bytes = 0
    parsed_ok = 0
    parsed_err = 0
    skipped_large = 0

    total_loc = 0
    total_sloc = 0
    total_comment_lines = 0
    total_blank_lines = 0

    for d in discovered:
        src_id = canon_web_file_id(d.rel_posix, repo_root)

        t_parse = time.perf_counter()
        try:
            data = d.abs_path.read_bytes()
            size_bytes = len(data)
            total_bytes += size_bytes

            if cfg.max_bytes_per_file and size_bytes > cfg.max_bytes_per_file:
                skipped_large += 1
                file_facts.append(
                    FileFacts(
                        **_filefacts_kwargs_with_metrics(
                            rel_posix=src_id,
                            loc=0,
                            sloc=0,
                            comment_lines=0,
                            blank_lines=0,
                            comment_pct=None,
                            parse_status="skipped_too_large",
                            imports=[],
                            exports=[],
                            functions=[],
                            classes=[],
                            globals=[],
                            facts={"facts": {"size_bytes": size_bytes}},
                            crosstalk_candidates_ts_v1=[],
                        )
                    )
                )
                raw_imports_by_file[src_id] = []
                continue

            tree = runtime.parse(rel_path=src_id, source_bytes=data)

            ris, cands = extract_imports_and_crosstalk(tree=tree, source=data)
            raw_imports_by_file[src_id] = ris

            syms = extract_js_symbols(tree=tree, source=data)

            text = data.decode("utf-8", errors="replace")
            metrics = _count_ts_js_metrics(text)

            loc = int(metrics["loc"] or 0)
            sloc = int(metrics["sloc"] or 0)
            comment_lines = int(metrics["comment_lines"] or 0)
            blank_lines = int(metrics["blank_lines"] or 0)
            comment_pct = metrics["comment_pct"]
            comment_pct_f = float(comment_pct) if comment_pct is not None else None

            total_loc += loc
            total_sloc += sloc
            total_comment_lines += comment_lines
            total_blank_lines += blank_lines

            # Keep this as raw syntax imports only.
            imports_specs = [r.spec for r in ris]

            syms_facts = dict(getattr(syms, "facts", {}) or {})
            syms_facts["size_bytes"] = size_bytes
            syms_facts["loc"] = loc
            syms_facts["sloc"] = sloc
            syms_facts["comment_lines"] = comment_lines
            syms_facts["blank_lines"] = blank_lines
            syms_facts["comment_pct"] = comment_pct_f
            syms_facts["parse_ms"] = int((time.perf_counter() - t_parse) * 1000)

            parsed_ok += 1
            file_facts.append(
                FileFacts(
                    **_filefacts_kwargs_with_metrics(
                        rel_posix=src_id,
                        loc=loc,
                        sloc=sloc,
                        comment_lines=comment_lines,
                        blank_lines=blank_lines,
                        comment_pct=comment_pct_f,
                        parse_status="ok",
                        imports=imports_specs,
                        exports=syms.exports,
                        functions=syms.functions,
                        classes=syms.classes,
                        globals=syms.globals,
                        facts={"facts": syms_facts},
                        crosstalk_candidates_ts_v1=cands or [],
                    )
                )
            )

        except Exception as e:
            print("TS PARSE ERROR:", src_id, type(e).__name__, repr(e))
            parsed_err += 1
            file_facts.append(
                FileFacts(
                    **_filefacts_kwargs_with_metrics(
                        rel_posix=src_id,
                        loc=0,
                        sloc=0,
                        comment_lines=0,
                        blank_lines=0,
                        comment_pct=None,
                        parse_status="parse_error",
                        imports=[],
                        exports=[],
                        functions=[],
                        classes=[],
                        globals=[],
                        facts={
                            "facts": {
                                "error": repr(e),
                                "parse_ms": int((time.perf_counter() - t_parse) * 1000),
                            }
                        },
                        crosstalk_candidates_ts_v1=[],
                    )
                )
            )
            raw_imports_by_file[src_id] = []

    comment_pct_total = (total_comment_lines / total_loc) if total_loc else None

    log_event(
        "TS:parse_summary",
        discovered=len(discovered),
        facts=len(file_facts),
        parsed_ok=parsed_ok,
        parsed_err=parsed_err,
        skipped_large=skipped_large,
        total_bytes=total_bytes,
        total_loc=total_loc,
        total_sloc=total_sloc,
        total_comment_lines=total_comment_lines,
        total_blank_lines=total_blank_lines,
        comment_pct=comment_pct_total,
    )

    resolved_all: List[ResolvedImport] = []
    resolved_by_src: Dict[str, List[ResolvedImport]] = {}

    pm_near_cache: Dict[str, Optional[TSPathMap]] = {}

    def _pathmaps_for_src(src: str) -> Sequence[TSPathMap]:
        out: List[TSPathMap] = []
        if pm_root is not None:
            out.append(pm_root)

        if src not in pm_near_cache:
            pm_near_cache[src] = get_pathmap_for_file(repo_root=repo_root, src_file_rel_posix=src)

        pm_near = pm_near_cache[src]
        if pm_near is None:
            return out

        if not out:
            out.append(pm_near)
            return out

        if not _same_pathmap(pm_near, out[0]):
            out.append(pm_near)

        return out

    t_resolve = time.perf_counter()
    for ff in file_facts:
        src = ff.rel_posix
        ris = raw_imports_by_file.get(src, [])
        file_pathmaps = _pathmaps_for_src(src)

        for r in ris:
            dst = _safe_resolve_spec(
                repo_root=repo_root,
                src=src,
                spec=r.spec,
                cfg=cfg,
                file_pathmaps=file_pathmaps,
            )

            if dst and not is_repo_relative(dst):
                dst = None

            ri = ResolvedImport(
                src_node_id=src,
                spec=r.spec,
                kind=r.kind,
                symbols=r.symbols,
                dst_node_id=dst,
                unresolved=(dst is None),
                loc=r.loc,
            )
            resolved_all.append(ri)
            resolved_by_src.setdefault(src, []).append(ri)

    unresolved_before = sum(1 for ri in resolved_all if ri.unresolved)
    total_before = len(resolved_all)

    if getattr(cfg, "dedup_edges", True):
        seen = set()
        deduped: List[ResolvedImport] = []
        for ri in resolved_all:
            spec_key = strip_query_and_hash((ri.spec or "").strip())
            if len(spec_key) > 1024:
                spec_key = spec_key[:1024]
            key = (ri.src_node_id, ri.dst_node_id or "", ri.kind, spec_key)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(ri)
        resolved_all = deduped

        rebuilt: Dict[str, List[ResolvedImport]] = {}
        for ri in resolved_all:
            rebuilt.setdefault(ri.src_node_id, []).append(ri)
        resolved_by_src = rebuilt

    unresolved_after = sum(1 for ri in resolved_all if ri.unresolved)
    log_event(
        "TS:resolve_summary",
        ms=int((time.perf_counter() - t_resolve) * 1000),
        resolved_total_before=total_before,
        unresolved_before=unresolved_before,
        resolved_total_after=len(resolved_all),
        unresolved_after=unresolved_after,
        dedup=(getattr(cfg, "dedup_edges", True)),
    )

    _summarize_resolved_imports(
        file_facts=file_facts,
        resolved_by_src=resolved_by_src,
        unresolved_after=unresolved_after,
    )

    referenced: set[str] = set()
    for ri in resolved_all:
        if ri.src_node_id:
            referenced.add(ri.src_node_id)
        if ri.dst_node_id:
            referenced.add(ri.dst_node_id)

    t_art = time.perf_counter()
    nodefacts = build_nodefacts(
        files=file_facts,
        repo_root=repo_root,
        extra_node_ids=sorted(referenced),
        resolved_by_src=resolved_by_src,
    )
    edges = build_edges(resolved=resolved_all, repo_root=repo_root)
    folder_index = build_folder_index(files=file_facts, resolved_by_src=resolved_by_src, repo_root=repo_root)

    enrich_nodefacts_with_degrees(nodefacts, edges)

    write_json(artifact_dir / cfg.nodefacts_name, nodefacts)
    write_json(artifact_dir / cfg.edges_name, edges)
    write_json(artifact_dir / cfg.folder_index_name, folder_index)

    log_event(
        "TS:artifacts_written",
        ms=int((time.perf_counter() - t_art) * 1000),
        nodefacts=str(artifact_dir / cfg.nodefacts_name),
        edges=str(artifact_dir / cfg.edges_name),
        folder_index=str(artifact_dir / cfg.folder_index_name),
        nodes=(len(nodefacts.get("nodes", [])) if isinstance(nodefacts, dict) else None),
        edges_n=(len(edges.get("edges", [])) if isinstance(edges, dict) else None),
    )

    log_event("TS:analyze_repo_end", total_ms=int((time.perf_counter() - t_all) * 1000))


def enrich_nodefacts_with_degrees(nodefacts, edges):
    """
    Enrich nodefacts with degree metrics computed from edges.
    Modifies nodefacts in-place.

    dependencies_count:
        How many files this file imports.

    importers_count:
        How many files import this file.
    """
    nodes = nodefacts.get("nodes", {})
    edge_list = edges.get("edges", [])

    importers_count = {}
    for e in edge_list:
        if isinstance(e, dict) and e.get("dst"):
            dst = e["dst"]
            importers_count[dst] = importers_count.get(dst, 0) + 1

    for node_id, node in nodes.items():
        node["dependencies_count"] = len(node.get("imports", []))
        node["importers_count"] = importers_count.get(node_id, 0)


def analyze_files_ts(
    *,
    repo_root: Path,
    artifact_dir: Path,
    cfg: TSAnalyzerCfg,
    files: Sequence[Path],
) -> Dict[str, object]:
    """
    Bucket/file-list entrypoint.
    """
    t_all = time.perf_counter()

    repo_root = repo_root.resolve()
    artifact_dir = artifact_dir.resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)

    log_event("TS:analyze_files_begin", repo_root=str(repo_root), artifact_dir=str(artifact_dir), files_in=len(files))

    t0 = time.perf_counter()
    pm_root = load_effective_repo_tsconfig_paths(repo_root)
    log_event("TS:tsconfig_root_loaded", ok=(pm_root is not None), ms=int((time.perf_counter() - t0) * 1000))

    rel_posix_list: List[str] = []
    outside = 0
    missing = 0
    for p in files:
        try:
            ap = p.resolve()
        except Exception:
            continue
        if not ap.exists() or not ap.is_file():
            missing += 1
            continue
        try:
            rel = ap.relative_to(repo_root).as_posix()
        except Exception:
            outside += 1
            continue
        rel_posix_list.append(rel)

    if cfg.skip_d_ts:
        before = len(rel_posix_list)
        rel_posix_list = [r for r in rel_posix_list if not r.endswith(".d.ts")]
        log_event("TS:skip_d_ts_applied", before=before, after=len(rel_posix_list))

    log_event("TS:files_normalized", files_used=len(rel_posix_list), skipped_outside=outside, skipped_missing=missing)

    runtime = TSRuntime()

    file_facts: List[FileFacts] = []
    raw_imports_by_file: Dict[str, List[RawImport]] = {}

    total_bytes = 0
    parsed_ok = 0
    parsed_err = 0
    skipped_large = 0

    total_loc = 0
    total_sloc = 0
    total_comment_lines = 0
    total_blank_lines = 0

    for rel_posix in rel_posix_list:
        abs_path = repo_root / rel_posix
        src_id = canon_web_file_id(rel_posix, repo_root)

        t_parse = time.perf_counter()
        try:
            data = abs_path.read_bytes()
            size_bytes = len(data)
            total_bytes += size_bytes

            if cfg.max_bytes_per_file and size_bytes > cfg.max_bytes_per_file:
                skipped_large += 1
                file_facts.append(
                    FileFacts(
                        **_filefacts_kwargs_with_metrics(
                            rel_posix=src_id,
                            loc=0,
                            sloc=0,
                            comment_lines=0,
                            blank_lines=0,
                            comment_pct=None,
                            parse_status="skipped_too_large",
                            imports=[],
                            exports=[],
                            functions=[],
                            classes=[],
                            globals=[],
                            facts={"facts": {"size_bytes": size_bytes}},
                            crosstalk_candidates_ts_v1=[],
                        )
                    )
                )
                raw_imports_by_file[src_id] = []
                continue

            tree = runtime.parse(rel_path=src_id, source_bytes=data)

            ris, cands = extract_imports_and_crosstalk(tree=tree, source=data)
            raw_imports_by_file[src_id] = ris

            syms = extract_js_symbols(tree=tree, source=data)

            text = data.decode("utf-8", errors="replace")
            metrics = _count_ts_js_metrics(text)

            loc = int(metrics["loc"] or 0)
            sloc = int(metrics["sloc"] or 0)
            comment_lines = int(metrics["comment_lines"] or 0)
            blank_lines = int(metrics["blank_lines"] or 0)
            comment_pct = metrics["comment_pct"]
            comment_pct_f = float(comment_pct) if comment_pct is not None else None

            total_loc += loc
            total_sloc += sloc
            total_comment_lines += comment_lines
            total_blank_lines += blank_lines

            # Keep this as raw syntax imports only.
            imports_specs = [r.spec for r in ris]

            syms_facts = dict(getattr(syms, "facts", {}) or {})
            syms_facts["size_bytes"] = size_bytes
            syms_facts["loc"] = loc
            syms_facts["sloc"] = sloc
            syms_facts["comment_lines"] = comment_lines
            syms_facts["blank_lines"] = blank_lines
            syms_facts["comment_pct"] = comment_pct_f
            syms_facts["parse_ms"] = int((time.perf_counter() - t_parse) * 1000)

            parsed_ok += 1
            file_facts.append(
                FileFacts(
                    **_filefacts_kwargs_with_metrics(
                        rel_posix=src_id,
                        loc=loc,
                        sloc=sloc,
                        comment_lines=comment_lines,
                        blank_lines=blank_lines,
                        comment_pct=comment_pct_f,
                        parse_status="ok",
                        imports=imports_specs,
                        exports=syms.exports,
                        functions=syms.functions,
                        classes=syms.classes,
                        globals=syms.globals,
                        facts={"facts": syms_facts},
                        crosstalk_candidates_ts_v1=cands or [],
                    )
                )
            )

        except Exception as e:
            print("TS PARSE ERROR:", src_id, type(e).__name__, repr(e))
            parsed_err += 1
            file_facts.append(
                FileFacts(
                    **_filefacts_kwargs_with_metrics(
                        rel_posix=src_id,
                        loc=0,
                        sloc=0,
                        comment_lines=0,
                        blank_lines=0,
                        comment_pct=None,
                        parse_status="parse_error",
                        imports=[],
                        exports=[],
                        functions=[],
                        classes=[],
                        globals=[],
                        facts={
                            "facts": {
                                "error": repr(e),
                                "parse_ms": int((time.perf_counter() - t_parse) * 1000),
                            }
                        },
                        crosstalk_candidates_ts_v1=[],
                    )
                )
            )
            raw_imports_by_file[src_id] = []

    comment_pct_total = (total_comment_lines / total_loc) if total_loc else None

    log_event(
        "TS:parse_summary",
        files_used=len(rel_posix_list),
        facts=len(file_facts),
        parsed_ok=parsed_ok,
        parsed_err=parsed_err,
        skipped_large=skipped_large,
        total_bytes=total_bytes,
        total_loc=total_loc,
        total_sloc=total_sloc,
        total_comment_lines=total_comment_lines,
        total_blank_lines=total_blank_lines,
        comment_pct=comment_pct_total,
    )

    resolved_all: List[ResolvedImport] = []
    resolved_by_src: Dict[str, List[ResolvedImport]] = {}

    pm_near_cache: Dict[str, Optional[TSPathMap]] = {}

    def _pathmaps_for_src(src: str) -> Sequence[TSPathMap]:
        out: List[TSPathMap] = []
        if pm_root is not None:
            out.append(pm_root)

        if src not in pm_near_cache:
            pm_near_cache[src] = get_pathmap_for_file(repo_root=repo_root, src_file_rel_posix=src)

        pm_near = pm_near_cache[src]
        if pm_near is None:
            return out

        if not out:
            out.append(pm_near)
            return out

        if not _same_pathmap(pm_near, out[0]):
            out.append(pm_near)

        return out

    t_resolve = time.perf_counter()
    for ff in file_facts:
        src = ff.rel_posix
        ris = raw_imports_by_file.get(src, [])
        file_pathmaps = _pathmaps_for_src(src)

        for r in ris:
            dst = _safe_resolve_spec(
                repo_root=repo_root,
                src=src,
                spec=r.spec,
                cfg=cfg,
                file_pathmaps=file_pathmaps,
            )

            if dst and not is_repo_relative(dst):
                dst = None

            ri = ResolvedImport(
                src_node_id=src,
                spec=r.spec,
                kind=r.kind,
                symbols=r.symbols,
                dst_node_id=dst,
                unresolved=(dst is None),
                loc=r.loc,
            )

            resolved_all.append(ri)
            resolved_by_src.setdefault(src, []).append(ri)

    unresolved_before = sum(1 for ri in resolved_all if ri.unresolved)
    total_before = len(resolved_all)

    if getattr(cfg, "dedup_edges", True):
        seen = set()
        deduped: List[ResolvedImport] = []
        for ri in resolved_all:
            spec_key = strip_query_and_hash((ri.spec or "").strip())
            if len(spec_key) > 1024:
                spec_key = spec_key[:1024]
            key = (ri.src_node_id, ri.dst_node_id or "", ri.kind, spec_key)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(ri)
        resolved_all = deduped

        rebuilt: Dict[str, List[ResolvedImport]] = {}
        for ri in resolved_all:
            rebuilt.setdefault(ri.src_node_id, []).append(ri)
        resolved_by_src = rebuilt

    unresolved_after = sum(1 for ri in resolved_all if ri.unresolved)
    log_event(
        "TS:resolve_summary",
        ms=int((time.perf_counter() - t_resolve) * 1000),
        resolved_total_before=total_before,
        unresolved_before=unresolved_before,
        resolved_total_after=len(resolved_all),
        unresolved_after=unresolved_after,
        dedup=(getattr(cfg, "dedup_edges", True)),
    )

    _summarize_resolved_imports(
        file_facts=file_facts,
        resolved_by_src=resolved_by_src,
        unresolved_after=unresolved_after,
    )

    referenced: set[str] = set()
    for ri in resolved_all:
        if ri.src_node_id:
            referenced.add(ri.src_node_id)
        if ri.dst_node_id:
            referenced.add(ri.dst_node_id)

    t_art = time.perf_counter()
    nodefacts = build_nodefacts(
        files=file_facts,
        repo_root=repo_root,
        extra_node_ids=sorted(referenced),
        resolved_by_src=resolved_by_src,
    )
    edges = build_edges(resolved=resolved_all, repo_root=repo_root)
    folder_index = build_folder_index(files=file_facts, resolved_by_src=resolved_by_src, repo_root=repo_root)

    write_json(artifact_dir / cfg.nodefacts_name, nodefacts)
    write_json(artifact_dir / cfg.edges_name, edges)
    write_json(artifact_dir / cfg.folder_index_name, folder_index)

    nodes_obj = nodefacts.get("nodes") if isinstance(nodefacts, dict) else None
    edges_list = edges.get("edges") if isinstance(edges, dict) else None

    nodes_dict = nodes_obj if isinstance(nodes_obj, dict) else {}
    edges_out = edges_list if isinstance(edges_list, list) else []

    log_event(
        "TS:fragment_ready",
        nodes=len(nodes_dict),
        edges=len(edges_out),
        ms=int((time.perf_counter() - t_art) * 1000),
    )

    return {
        "nodes": nodes_dict if isinstance(nodes_dict, dict) else {},
        "edges": edges_out if isinstance(edges_out, list) else [],
        "meta": {
            "analyzer": "ts",
            "files_in": len(files),
            "files_used": len(rel_posix_list),
            "parsed_ok": parsed_ok,
            "parsed_err": parsed_err,
            "skipped_large": skipped_large,
            "total_bytes": total_bytes,
            "total_loc": total_loc,
            "total_sloc": total_sloc,
            "total_comment_lines": total_comment_lines,
            "total_blank_lines": total_blank_lines,
            "comment_pct": comment_pct_total,
            "artifact_dir": str(artifact_dir),
        },
    }