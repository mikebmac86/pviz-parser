# backend/saas_analyzer/analyzer/go/build_artifacts.py
from __future__ import annotations

"""
Go artifact builders (FolderIndex -> NodeFacts + Edges).

This module must NOT invent its own normalization rules.
All normalization/classification/resolution lives in:
  - analyzer/go/canonical_go.py

Inputs:
  - Go FolderIndex (FolderIndex.files: "<pkgid>::<relfile>" -> FileEntry)
    where FileEntry.id is the package-id (import path) and
    FileEntry.imports_internal is already canonicalized package-id space.

Outputs:
  - NodeFacts object
    - scc_id / scc_size are computed from the Go package dependency graph
  - Edges object (edges@v1 compatible shape)
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from analyzer_store.types import FileEntry, FolderIndex, NODEFACTS_SCHEMA

from analyzer.go.go_canonical import (
    normalize_import_spec,
    normalize_module_path,
    resolve_go_import,
    internal_pkg_ids_from_folder_index,
    build_internal_edges_from_folder_index,
)

from analyzer.go.go_nodefacts_symbols import parse_symbols_for_nodefacts

try:
    from diagnostics.logging import log_event  # type: ignore
except Exception:  # pragma: no cover
    def log_event(*_a, **_k):
        return

def _stable_unique_strs(vals: Iterable[str]) -> Tuple[str, ...]:
    s: Set[str] = set()
    for v in vals or ():
        if not v:
            continue
        vv = str(v).strip()
        if vv:
            s.add(vv)
    return tuple(sorted(s))


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def _safe_nonnegative_int(value: Any, default: int = 0) -> int:
    return max(_safe_int(value, default), 0)


def _comment_pct(comment_lines: int, loc: int) -> Optional[float]:
    return (comment_lines / loc) if loc else None


def _upgrade_parse_status(cur: str, new: str) -> str:
    cur = (cur or "").strip() or "ok"
    new = (new or "").strip() or "ok"
    rank = {"ok": 0, "warn": 1, "error": 2}
    return new if rank.get(new, 0) > rank.get(cur, 0) else cur


def _compute_scc_from_deps(
    node_ids: Iterable[str],
    deps_by_node: Dict[str, Set[str]],
) -> Tuple[Dict[str, str], Dict[str, int]]:
    index = 0
    current_scc = 0
    stack: List[str] = []
    on_stack: Set[str] = set()
    indices: Dict[str, int] = {}
    lowlink: Dict[str, int] = {}
    scc_id_by_node: Dict[str, str] = {}
    scc_size_by_id: Dict[str, int] = {}

    def strongconnect(v: str) -> None:
        nonlocal index, current_scc

        indices[v] = index
        lowlink[v] = index
        index += 1

        stack.append(v)
        on_stack.add(v)

        for w in deps_by_node.get(v, set()):
            if w not in indices:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], indices[w])

        if lowlink[v] == indices[v]:
            sid = str(current_scc)
            size = 0

            while True:
                w = stack.pop()
                on_stack.discard(w)
                scc_id_by_node[w] = sid
                size += 1

                if w == v:
                    break

            scc_size_by_id[sid] = size
            current_scc += 1

    all_nodes: Set[str] = set(str(n) for n in node_ids if str(n).strip())

    for src, deps in deps_by_node.items():
        if src:
            all_nodes.add(src)
        for dst in deps or ():
            if dst:
                all_nodes.add(dst)

    for nid in sorted(all_nodes):
        if nid not in indices:
            strongconnect(nid)

    return scc_id_by_node, scc_size_by_id


def _pkg_name_from_id(pkg_id: str) -> str:
    s = normalize_import_spec(pkg_id)
    if not s:
        return ""
    return s.rsplit("/", 1)[-1]


def _stub_pkg_node(pkg_id: str) -> Dict[str, Any]:
    pid = normalize_import_spec(pkg_id)
    return {
        "id": pid,
        "name": _pkg_name_from_id(pid) or pid,
        "language": "go",
        "lang": "go",
        "imports": (),
        "imports_all_raw": (),
        "imports_external": (),
        "language_facts": {},
        "exports": (),
        "classes": (),
        "functions": (),
        "globals": (),
        "gen_seq": 0,
        "importers_count": 0,
        "dependencies_count": 0,
        "scc_id": "",
        "scc_size": 1,
        "file": "",
        "hash": None,

        "loc": None,
        "sloc": None,
        "comment_lines": None,
        "blank_lines": None,
        "comment_pct": None,

        "file_count": 0,
        "size_bytes": None,
        "mtime": None,
        "du": None,
        "dd": None,
        "parse_status": "warn",
        "crosstalk_candidates_go_v1": (),
    }


def _chunked(seq: Sequence[Path], n: int) -> Iterable[List[Path]]:
    if n <= 0:
        yield list(seq)
        return
    buf: List[Path] = []
    for p in seq:
        buf.append(p)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf


def _maybe_preload_go_ast_batch(
    *,
    repo_root: Path,
    files: Sequence[FileEntry],
    cfg: Any,
) -> None:
    try:
        enable = bool(getattr(cfg, "enable_go_ast", False))
        if not enable:
            return
    except Exception:
        return

    try:
        from analyzer.go.go_parse_dispatch import get_go_batch_parser
    except Exception:
        return

    try:
        include_docs = bool(getattr(cfg, "goextract_include_docs", True))
        include_imports = bool(getattr(cfg, "goextract_include_imports", True))
        include_build = bool(getattr(cfg, "goextract_include_build", True))
        timeout_s = int(getattr(cfg, "goextract_timeout_s", 180) or 180)
        batch_size = int(getattr(cfg, "goextract_batch_size", 0) or 0)
        goextract_bin = getattr(cfg, "goextract_bin", None)
    except Exception:
        include_docs, include_imports, include_build, timeout_s, batch_size, goextract_bin = True, True, True, 180, 0, None

    abs_paths: List[Path] = []
    for fe in files:
        try:
            rel = str(getattr(fe, "file", "") or "").strip()
            if not rel or not rel.lower().endswith(".go"):
                continue

            eligible = bool(getattr(fe, "eligible", True))
            allow_oversize = bool(getattr(cfg, "allow_oversize_files", False))
            if not eligible and not allow_oversize:
                continue

            abs_paths.append((repo_root / rel).resolve(strict=False))
        except Exception:
            continue

    if not abs_paths:
        return

    try:
        bp = get_go_batch_parser()

        try:
            if goextract_bin:
                bp.helper_bin = Path(goextract_bin)  # type: ignore[attr-defined]
        except Exception:
            pass

        total = 0
        for chunk in _chunked(abs_paths, batch_size):
            bp.load_batch(
                chunk,
                include_docs=include_docs,
                include_imports=include_imports,
                include_build=include_build,
                timeout_s=timeout_s,
            )
            total += len(chunk)

        log_event(
            "GO:ast_batch_preloaded",
            files=total,
            docs=bool(include_docs),
            imports=bool(include_imports),
            build=bool(include_build),
            chunk_size=int(batch_size or 0),
        )
    except Exception as e:
        log_event("GO:ast_batch_preload_failed", err=f"{type(e).__name__}: {e}"[:200])


def build_nodefacts_from_folder_index(
    idx: FolderIndex,
    *,
    repo_root: Path,
    cfg: Any,
    module_path: Optional[str] = None,
) -> Dict[str, Any]:
    t0 = None
    try:
        import time
        t0 = time.perf_counter()
    except Exception:
        t0 = None

    repo_root = Path(repo_root).absolute()
    mp = normalize_module_path(module_path)

    files_map = getattr(idx, "files", {}) or {}
    files_list: List[FileEntry] = [fe for fe in files_map.values() if isinstance(fe, FileEntry)]

    _maybe_preload_go_ast_batch(repo_root=repo_root, files=files_list, cfg=cfg)

    pkg_loc_total: Dict[str, int] = {}
    pkg_sloc_total: Dict[str, int] = {}
    pkg_comment_lines_total: Dict[str, int] = {}
    pkg_blank_lines_total: Dict[str, int] = {}
    pkg_file_count: Dict[str, int] = {}
    pkg_imports_all_raw: Dict[str, Set[str]] = {}
    pkg_imports_external: Dict[str, Set[str]] = {}
    by_pkg: Dict[str, Dict[str, Any]] = {}
    pkg_rep_file: Dict[str, str] = {}

    parsed_ok = 0
    parsed_warn = 0
    parsed_err = 0

    for fe in files_list:
        pkg_id = normalize_import_spec(getattr(fe, "id", "") or "")
        rel_file = str(getattr(fe, "file", "") or "").strip()
        if not pkg_id or not rel_file:
            continue

        fe_loc = _safe_nonnegative_int(getattr(fe, "loc", None), 0)
        fe_sloc = _safe_nonnegative_int(getattr(fe, "sloc", None), 0)
        fe_comment_lines = _safe_nonnegative_int(getattr(fe, "comment_lines", None), 0)
        fe_blank_lines = _safe_nonnegative_int(getattr(fe, "blank_lines", None), 0)

        pkg_loc_total[pkg_id] = pkg_loc_total.get(pkg_id, 0) + fe_loc
        pkg_sloc_total[pkg_id] = pkg_sloc_total.get(pkg_id, 0) + fe_sloc
        pkg_comment_lines_total[pkg_id] = pkg_comment_lines_total.get(pkg_id, 0) + fe_comment_lines
        pkg_blank_lines_total[pkg_id] = pkg_blank_lines_total.get(pkg_id, 0) + fe_blank_lines
        pkg_file_count[pkg_id] = pkg_file_count.get(pkg_id, 0) + 1

        raw_acc = pkg_imports_all_raw.setdefault(pkg_id, set())
        for raw in (getattr(fe, "imports_all", None) or ()):
            spec = normalize_import_spec(raw or "")
            if spec:
                raw_acc.add(spec)

        ext_acc = pkg_imports_external.setdefault(pkg_id, set())
        for raw in (getattr(fe, "imports_external", None) or ()):
            spec = normalize_import_spec(raw or "")
            if spec:
                ext_acc.add(spec)

        abs_path = (repo_root / rel_file).absolute()
        syms = parse_symbols_for_nodefacts(abs_path, cfg)

        status = _upgrade_parse_status(
            str(getattr(fe, "parse_status", "ok") or "ok"),
            syms.parse_status,
        )

        if status == "ok":
            parsed_ok += 1
        elif status == "warn":
            parsed_warn += 1
        else:
            parsed_err += 1

        if pkg_id not in by_pkg:
            by_pkg[pkg_id] = {
                "id": pkg_id,
                "name": _pkg_name_from_id(pkg_id) or pkg_id,
                "language": "go",
                "lang": "go",
                "imports": (),
                "exports": (),
                "imports_all_raw": (),
                "imports_external": (),
                "language_facts": {},
                "classes": (),
                "functions": (),
                "globals": (),
                "gen_seq": 0,
                "importers_count": 0,
                "dependencies_count": 0,
                "scc_id": "",
                "scc_size": 1,
                "file": rel_file,
                "hash": getattr(fe, "hash", None),

                "loc": 0,
                "sloc": 0,
                "comment_lines": 0,
                "blank_lines": 0,
                "comment_pct": None,

                "file_count": 0,
                "size_bytes": getattr(fe, "size_bytes", None),
                "mtime": getattr(fe, "mtime", None),
                "du": None,
                "dd": None,
                "parse_status": status,
                "crosstalk_candidates_go_v1": (),
            }
            pkg_rep_file[pkg_id] = rel_file
        else:
            node0 = by_pkg[pkg_id]
            node0["parse_status"] = _upgrade_parse_status(node0.get("parse_status", "ok"), status)

            try:
                cur = pkg_rep_file.get(pkg_id) or node0.get("file") or ""
                if rel_file and (not cur or rel_file < cur):
                    pkg_rep_file[pkg_id] = rel_file
                    node0["file"] = rel_file
            except Exception:
                pass

            try:
                if node0.get("size_bytes") is None and getattr(fe, "size_bytes", None) is not None:
                    node0["size_bytes"] = getattr(fe, "size_bytes", None)
            except Exception:
                pass

            try:
                if node0.get("mtime") is None and getattr(fe, "mtime", None) is not None:
                    node0["mtime"] = getattr(fe, "mtime", None)
            except Exception:
                pass

        node = by_pkg[pkg_id]
        node["exports"] = _stable_unique_strs(tuple(node.get("exports", ())) + tuple(syms.exports))
        node["classes"] = _stable_unique_strs(tuple(node.get("classes", ())) + tuple(syms.classes))
        node["functions"] = _stable_unique_strs(tuple(node.get("functions", ())) + tuple(syms.functions))
        node["globals"] = _stable_unique_strs(tuple(node.get("globals", ())) + tuple(syms.globals))

    deps_by_pkg: Dict[str, Set[str]] = {}

    for fe in files_list:
        src = normalize_import_spec(getattr(fe, "id", "") or "")
        if not src:
            continue
        deps = deps_by_pkg.setdefault(src, set())

        for dst0 in (getattr(fe, "imports_internal", None) or ()):
            dst = normalize_import_spec(dst0 or "")
            if not dst or dst == src:
                continue
            deps.add(dst)

    importers_count: Dict[str, int] = {}
    for src, deps in deps_by_pkg.items():
        for dst in deps:
            importers_count[dst] = importers_count.get(dst, 0) + 1

    for src, deps in deps_by_pkg.items():
        if src not in by_pkg:
            by_pkg[src] = _stub_pkg_node(src)
        for dst in deps:
            if dst not in by_pkg:
                by_pkg[dst] = _stub_pkg_node(dst)

    scc_id_by_node, scc_size_by_id = _compute_scc_from_deps(
        by_pkg.keys(),
        deps_by_pkg,
    )

    for pkg_id, node in by_pkg.items():
        deps = deps_by_pkg.get(pkg_id, set())

        loc_total = int(pkg_loc_total.get(pkg_id, 0))
        comment_total = int(pkg_comment_lines_total.get(pkg_id, 0))

        node["imports"] = tuple(sorted(deps))
        imports_all_raw = tuple(sorted(pkg_imports_all_raw.get(pkg_id, set())))
        imports_external = tuple(sorted(pkg_imports_external.get(pkg_id, set())))

        node["imports_all_raw"] = imports_all_raw
        node["imports_external"] = imports_external
        node["dependencies_count"] = len(deps)
        node["importers_count"] = importers_count.get(pkg_id, 0)

        sid = scc_id_by_node.get(pkg_id, pkg_id)
        node["scc_id"] = sid
        node["scc_size"] = scc_size_by_id.get(sid, 1)

        node["loc"] = loc_total
        node["sloc"] = int(pkg_sloc_total.get(pkg_id, 0))
        node["comment_lines"] = comment_total
        node["blank_lines"] = int(pkg_blank_lines_total.get(pkg_id, 0))
        node["comment_pct"] = _comment_pct(comment_total, loc_total)

        node["file_count"] = int(pkg_file_count.get(pkg_id, 0))

        go_facts: Dict[str, Any] = {
            "package_id": pkg_id,
        }

        if mp:
            go_facts["module_path"] = mp

        if node.get("file"):
            go_facts["representative_file"] = node.get("file")

        if imports_all_raw:
            go_facts["imports"] = list(imports_all_raw)

        if imports_external:
            go_facts["imports_external"] = list(imports_external)

        if node.get("exports"):
            go_facts["exports"] = list(node.get("exports") or ())

        if node.get("classes"):
            go_facts["classes"] = list(node.get("classes") or ())

        if node.get("functions"):
            go_facts["functions"] = list(node.get("functions") or ())

        if node.get("globals"):
            go_facts["globals"] = list(node.get("globals") or ())

        node["language_facts"] = {
            "go": go_facts,
        }

    nodes = {k: by_pkg[k] for k in sorted(by_pkg.keys())}

    loc_total_all = int(sum(pkg_loc_total.values()))
    sloc_total_all = int(sum(pkg_sloc_total.values()))
    comment_lines_total_all = int(sum(pkg_comment_lines_total.values()))
    blank_lines_total_all = int(sum(pkg_blank_lines_total.values()))
    nodes_with_raw_imports = sum(
        1 for node in nodes.values()
        if node.get("imports_all_raw")
    )
    raw_import_specs_total = sum(
        len(node.get("imports_all_raw") or ())
        for node in nodes.values()
    )
    nodes_with_external_imports = sum(
        1 for node in nodes.values()
        if node.get("imports_external")
    )
    external_import_specs_total = sum(
        len(node.get("imports_external") or ())
        for node in nodes.values()
    )
    nodes_with_language_facts = sum(
        1 for node in nodes.values()
        if node.get("language_facts")
    )
    out = {
        "schema_version": NODEFACTS_SCHEMA,
        "language": "go",
        "nodes": nodes,
        "meta": {
            "module_path": mp or "",
            "packages": len(nodes),
            "files_indexed": len(files_map),
            "parse_ok": parsed_ok,
            "parse_warn": parsed_warn,
            "parse_err": parsed_err,
            "nodes_with_imports_all_raw": int(nodes_with_raw_imports),
            "raw_import_specs_total": int(raw_import_specs_total),
            "nodes_with_imports_external": int(nodes_with_external_imports),
            "external_import_specs_total": int(external_import_specs_total),
            "nodes_with_language_facts": int(nodes_with_language_facts),
            "loc_total": loc_total_all,
            "sloc_total": sloc_total_all,
            "comment_lines_total": comment_lines_total_all,
            "blank_lines_total": blank_lines_total_all,
            "comment_pct": _comment_pct(comment_lines_total_all, loc_total_all),
        },
    }

    try:
        if t0 is not None:
            import time
            log_event(
                "GO:nodefacts_built",
                packages=len(nodes),
                loc_total=loc_total_all,
                sloc_total=sloc_total_all,
                comment_lines_total=comment_lines_total_all,
                blank_lines_total=blank_lines_total_all,
                nodes_with_imports_all_raw=int(nodes_with_raw_imports),
                raw_import_specs_total=int(raw_import_specs_total),
                external_import_specs_total=int(external_import_specs_total),
                nodes_with_language_facts=int(nodes_with_language_facts),
                ms=int((time.perf_counter() - t0) * 1000),
            )
    except Exception:
        pass

    return out


def build_edges_from_folder_index(
    idx: FolderIndex,
    *,
    module_path: Optional[str] = None,
    internal_only: bool = True,
    include_external: bool = False,
    drop_self_edges: bool = True,
) -> Dict[str, Any]:
    mp = normalize_module_path(module_path)

    edges_out: List[Dict[str, object]] = []
    seen: Set[Tuple[str, str, str]] = set()

    def _add_edge(
        src: str,
        dst: str,
        kind: str,
        confidence: float,
        *,
        label: Optional[str] = None,
        spec: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> None:
        if not src or not dst or not kind:
            return
        if drop_self_edges and src == dst:
            return

        key = (src, dst, kind)
        if key in seen:
            return

        seen.add(key)

        edge: Dict[str, object] = {
            "src": src,
            "dst": dst,
            "kind": kind,
            "confidence": float(confidence),
            "weight": float(confidence),
        }
        if label:
            edge["label"] = label
        if spec:
            edge["spec"] = spec
        if reason:
            edge["reason"] = reason

        edges_out.append(edge)

    files = getattr(idx, "files", {}) or {}

    if internal_only:
        pairs = build_internal_edges_from_folder_index(idx, drop_self_edges=drop_self_edges)
        for src, dst in pairs:
            kind = "go:import:internal"
            _add_edge(
                src,
                dst,
                kind,
                0.8,
                reason="go_import_internal",
            )

    if include_external:
        internal_ids = internal_pkg_ids_from_folder_index(idx)

        for _k, fe in files.items():
            src = normalize_import_spec(getattr(fe, "id", "") or "")
            if not src:
                continue

            for raw in (getattr(fe, "imports_all", None) or ()):
                spec = normalize_import_spec(raw or "")
                if not spec:
                    continue

                rr = resolve_go_import(
                    spec,
                    module_path=mp,
                    internal_pkg_ids=internal_ids,
                    allow_prefix_collapse=True,
                )

                if rr.kind == "internal":
                    continue

                kind = f"go:import:{rr.kind}"
                dst = spec

                _add_edge(
                    src,
                    dst,
                    kind,
                    0.4,
                    label=spec,
                    spec=spec,
                    reason=f"go_import_{rr.kind}",
                )

    edges_out.sort(key=lambda e: (str(e.get("kind", "")), str(e.get("src", "")), str(e.get("dst", ""))))

    out = {
        "schema_version": "edges@v1",
        "language": "go",
        "edges": edges_out,
        "meta": {
            "module_path": mp or "",
            "total": len(edges_out),
            "internal": sum(1 for e in edges_out if e.get("kind") == "go:import:internal"),
        },
    }
    return out