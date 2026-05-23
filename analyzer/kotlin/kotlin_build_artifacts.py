from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
import time

from analyzer_store.types import FileEntry, FolderIndex
from adapters.canonical import to_posix
from analyzer.kotlin.kotlin_nodefacts_symbols import NodeFactsSymbols, parse_symbols_for_nodefacts

try:
    from diagnostics.logging import log_event
except Exception:
    def log_event(*_a, **_k):
        return


def _upgrade_status(cur: str, new: str) -> str:
    rank = {"ok": 0, "warn": 1, "error": 2}
    c = cur or "ok"
    n = new or "ok"
    return n if rank.get(n, 0) > rank.get(c, 0) else c


def _name_from_id(node_id: str) -> str:
    s = str(node_id or "").strip()
    if "/" in s:
        return s.rsplit("/", 1)[-1]
    if "." in s:
        return s.rsplit(".", 1)[-1]
    return s


def _stub_node(node_id: str) -> Dict[str, Any]:
    return {
        "id": node_id,
        "name": _name_from_id(node_id),
        "language": "kotlin",
        "lang": "kotlin",
        "imports": (),
        "exports": (),
        "classes": (),
        "interfaces": (),
        "objects": (),
        "enums": (),
        "type_aliases": (),
        "functions": (),
        "globals": (),
        "annotations": (),
        "imports_all_raw": (),
        "imports_external": (),
        "gen_seq": 0,
        "importers_count": 0,
        "dependencies_count": 0,
        "scc_id": "",
        "scc_size": 1,
        "file": node_id,
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
        "crosstalk_candidates_kotlin_v1": (),
        "package": None,
        "declared_types": (),
        "declared_types_fq": (),
        "public_exports": (),
    }


def _cache_get(parse_cache: Dict[str, Any], *, rel_file: str, fe_id: str, repo_root: Path) -> Optional[Any]:
    for key in (rel_file, to_posix(rel_file), fe_id, to_posix(fe_id)):
        if key and key in parse_cache:
            return parse_cache[key]
    try:
        abs_key = str((repo_root / rel_file).resolve(strict=False))
        if abs_key in parse_cache:
            return parse_cache[abs_key]
    except Exception:
        pass
    return None


def build_nodefacts_from_folder_index(
    idx: FolderIndex,
    *,
    repo_root: Path,
    cfg: Any,
    module_name: Optional[str] = None,
) -> Dict[str, Any]:
    t0 = time.perf_counter()
    repo_root = Path(repo_root).resolve()
    files = getattr(idx, "files", {}) or {}

    node_id_space = str(getattr(cfg, "kotlin_node_id_space", "file") or "file").strip().lower()
    parse_cache = getattr(cfg, "kotlin_parse_cache", None)
    file_id_to_package: Dict[str, str] = dict(getattr(cfg, "kotlin_file_id_to_package", {}) or {})

    if node_id_space == "package" and not file_id_to_package:
        node_id_space = "file"

    fe_list: List[FileEntry] = [fe for fe in files.values() if isinstance(fe, FileEntry)]
    fe_list.sort(
        key=lambda fe: (
            to_posix(str(getattr(fe, "id", "") or "")),
            to_posix(str(getattr(fe, "file", "") or "")),
        )
    )

    by_node: Dict[str, Dict[str, Any]] = {}
    deps_by_node: Dict[str, Set[str]] = {}
    importers_count: Dict[str, int] = {}
    node_loc_total: Dict[str, int] = {}
    node_sloc_total: Dict[str, int] = {}
    node_comment_lines_total: Dict[str, int] = {}
    node_blank_lines_total: Dict[str, int] = {}
    node_file_count: Dict[str, int] = {}
    parsed_ok = parsed_warn = parsed_err = 0

    def _node_id_for_file(fe: FileEntry) -> str:
        fid = to_posix(str(getattr(fe, "id", "") or "").strip())
        return file_id_to_package.get(fid, "") if node_id_space == "package" else fid

    for fe in fe_list:
        rel_file = str(getattr(fe, "file", "") or "").strip()
        fe_id = to_posix(str(getattr(fe, "id", "") or "").strip())

        if not rel_file or not fe_id:
            continue

        cached = (
            _cache_get(parse_cache, rel_file=rel_file, fe_id=fe_id, repo_root=repo_root)
            if isinstance(parse_cache, dict)
            else None
        )

        syms = parse_symbols_for_nodefacts(
            cached if cached is not None else (repo_root / rel_file).resolve(strict=False),
            cfg,
        )

        node_id = _node_id_for_file(fe)
        if not node_id:
            continue

        status = _upgrade_status(str(getattr(fe, "parse_status", "ok") or "ok"), syms.parse_status)

        if status == "ok":
            parsed_ok += 1
        elif status == "warn":
            parsed_warn += 1
        else:
            parsed_err += 1

        fe_loc = int(getattr(fe, "loc", 0) or 0)
        fe_sloc = int(getattr(fe, "sloc", 0) or 0)
        fe_comment_lines = int(getattr(fe, "comment_lines", 0) or 0)
        fe_blank_lines = int(getattr(fe, "blank_lines", 0) or 0)

        node_loc_total[node_id] = node_loc_total.get(node_id, 0) + max(fe_loc, 0)
        node_sloc_total[node_id] = node_sloc_total.get(node_id, 0) + max(fe_sloc, 0)
        node_comment_lines_total[node_id] = node_comment_lines_total.get(node_id, 0) + max(fe_comment_lines, 0)
        node_blank_lines_total[node_id] = node_blank_lines_total.get(node_id, 0) + max(fe_blank_lines, 0)
        node_file_count[node_id] = node_file_count.get(node_id, 0) + 1

        if node_id not in by_node:
            by_node[node_id] = {
                "id": node_id,
                "name": _name_from_id(node_id),
                "language": "kotlin",
                "lang": "kotlin",
                "imports": (),
                "importers_count": 0,
                "dependencies_count": 0,
                "exports": (),
                "classes": (),
                "interfaces": (),
                "objects": (),
                "enums": (),
                "type_aliases": (),
                "functions": (),
                "globals": (),
                "annotations": (),
                "imports_all_raw": (),
                "imports_external": (),
                "package": syms.package,
                "declared_types": (),
                "declared_types_fq": (),
                "public_exports": (),
                "gen_seq": 0,
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
                "crosstalk_candidates_kotlin_v1": (),
                "__acc_exports": set(),
                "__acc_classes": set(),
                "__acc_interfaces": set(),
                "__acc_objects": set(),
                "__acc_enums": set(),
                "__acc_type_aliases": set(),
                "__acc_functions": set(),
                "__acc_globals": set(),
                "__acc_declared_types": set(),
                "__acc_declared_types_fq": set(),
                "__acc_public_exports": set(),
                "__acc_annotations": set(),
                "__acc_imports_all_raw": set(),
                "__acc_imports_external": set(),
            }
        else:
            by_node[node_id]["parse_status"] = _upgrade_status(
                by_node[node_id].get("parse_status", "ok"),
                status,
            )

        node = by_node[node_id]

        if syms.package:
            node["package"] = syms.package

        for k, vals in {
            "exports": syms.exports,
            "classes": syms.classes,
            "interfaces": syms.interfaces,
            "objects": syms.objects,
            "enums": syms.enums,
            "type_aliases": syms.type_aliases,
            "functions": syms.functions,
            "globals": syms.globals,
            "declared_types": syms.declared_types,
            "declared_types_fq": syms.declared_types_fq,
            "public_exports": syms.public_exports,
            "annotations": syms.annotations,
            "imports_all_raw": syms.imports_all_raw,
            "imports_external": syms.imports_external,
        }.items():
            acc = node.get(f"__acc_{k}")
            if isinstance(acc, set):
                for v in vals or ():
                    if str(v).strip():
                        acc.add(str(v).strip())

    for fe in fe_list:
        src_file = to_posix(str(getattr(fe, "id", "") or "").strip())
        src = file_id_to_package.get(src_file, "") if node_id_space == "package" else src_file

        if not src:
            continue

        deps = deps_by_node.setdefault(src, set())

        combined_deps: Set[str] = set()
        combined_deps.update(getattr(fe, "imports_internal", ()) or ())
        combined_deps.update(getattr(fe, "symbol_internal", ()) or ())

        for dst0 in combined_deps:
            dst_file = to_posix(str(dst0 or "").strip())
            dst = file_id_to_package.get(dst_file, "") if node_id_space == "package" else dst_file

            if dst and dst != src:
                deps.add(dst)
                importers_count[dst] = importers_count.get(dst, 0) + 1

    for src, deps in deps_by_node.items():
        by_node.setdefault(src, _stub_node(src))
        for dst in deps:
            by_node.setdefault(dst, _stub_node(dst))

    # ----------------------------------------
    # SCC COMPUTATION (Tarjan)
    # ----------------------------------------
    index = 0
    stack = []
    on_stack = set()
    indices = {}
    lowlink = {}
    scc_id_map = {}
    scc_sizes = {}
    current_scc = 0

    def strongconnect(v: str):
        nonlocal index, current_scc
        indices[v] = index
        lowlink[v] = index
        index += 1
        stack.append(v)
        on_stack.add(v)

        for w in deps_by_node.get(v, ()):
            if w not in indices:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], indices[w])

        if lowlink[v] == indices[v]:
            size = 0
            while True:
                w = stack.pop()
                on_stack.remove(w)
                scc_id_map[w] = str(current_scc)
                size += 1
                if w == v:
                    break
            scc_sizes[str(current_scc)] = size
            current_scc += 1

    for node_id in by_node.keys():
        if node_id not in indices:
            strongconnect(node_id)

    for nid, node in by_node.items():
        loc_total = int(node_loc_total.get(nid, 0))
        comment_total = int(node_comment_lines_total.get(nid, 0))

        node["loc"] = loc_total
        node["sloc"] = int(node_sloc_total.get(nid, 0))
        node["comment_lines"] = comment_total
        node["blank_lines"] = int(node_blank_lines_total.get(nid, 0))
        node["comment_pct"] = (comment_total / loc_total) if loc_total else None
        node["file_count"] = int(node_file_count.get(nid, 0))

    for node in by_node.values():
        for k in (
            "exports",
            "classes",
            "interfaces",
            "objects",
            "enums",
            "type_aliases",
            "functions",
            "globals",
            "declared_types",
            "declared_types_fq",
            "public_exports",
            "annotations",
            "imports_all_raw",
            "imports_external",
        ):
            acc = node.pop(f"__acc_{k}", None)
            if isinstance(acc, set):
                node[k] = tuple(sorted(acc))

    nodes = {k: by_node[k] for k in sorted(by_node)}

    loc_total_all = int(sum(node_loc_total.values()))
    sloc_total_all = int(sum(node_sloc_total.values()))
    comment_lines_total_all = int(sum(node_comment_lines_total.values()))
    blank_lines_total_all = int(sum(node_blank_lines_total.values()))
    comment_pct_total = (comment_lines_total_all / loc_total_all) if loc_total_all else None

    out = {
        "schema_version": "nodefacts@v1.6",
        "language": "kotlin",
        "nodes": nodes,
        "meta": {
            "module_name": module_name or "",
            "nodes": len(nodes),
            "files_indexed": len(files),
            "parse_ok": parsed_ok,
            "parse_warn": parsed_warn,
            "parse_err": parsed_err,
            "loc_total": loc_total_all,
            "sloc_total": sloc_total_all,
            "comment_lines_total": comment_lines_total_all,
            "blank_lines_total": blank_lines_total_all,
            "comment_pct": comment_pct_total,
            "kotlin_node_id_space": node_id_space,
            "used_parse_cache": bool(isinstance(parse_cache, dict) and parse_cache),
            "kotlin_file_id_to_package": dict(file_id_to_package),
        },
    }

    log_event("KOTLIN:nodefacts_built", nodes=len(nodes), ms=int((time.perf_counter() - t0) * 1000))
    return out


def build_edges_from_folder_index(
    idx: FolderIndex,
    *,
    internal_only: bool = True,
    include_external: bool = False,
    drop_self_edges: bool = True,
    cfg: Any = None,
) -> Dict[str, Any]:
    node_id_space = (
        str(getattr(cfg, "kotlin_node_id_space", "file") or "file").strip().lower()
        if cfg
        else "file"
    )
    file_id_to_package: Dict[str, str] = (
        dict(getattr(cfg, "kotlin_file_id_to_package", {}) or {})
        if cfg
        else {}
    )

    if node_id_space == "package" and not file_id_to_package:
        node_id_space = "file"

    def _map(fid: str) -> str:
        f = to_posix(str(fid or "").strip())
        return file_id_to_package.get(f, "") if node_id_space == "package" else f

    seen: Set[Tuple[str, str, str]] = set()
    edges: List[Dict[str, object]] = []

    for fe in (getattr(idx, "files", {}) or {}).values():
        if not isinstance(fe, FileEntry):
            continue

        src = _map(str(getattr(fe, "id", "") or ""))
        if not src:
            continue

        # -------------------------
        # IMPORT EDGES
        # -------------------------
        for dst0 in getattr(fe, "imports_internal", ()) or ():
            dst = _map(str(dst0 or ""))
            if not dst or (drop_self_edges and src == dst):
                continue

            kind = "kotlin:import:internal"
            key = (src, dst, kind)
            if key in seen:
                continue

            seen.add(key)
            edges.append({
                "src": src,
                "dst": dst,
                "kind": kind,
                "confidence": 0.5,
            })

        # -------------------------
        # SYMBOL EDGES
        # -------------------------
        for dst0 in getattr(fe, "symbol_internal", ()) or ():
            dst = _map(str(dst0 or ""))
            if not dst or (drop_self_edges and src == dst):
                continue

            kind = "kotlin:symbol:internal"
            key = (src, dst, kind)
            if key in seen:
                continue

            seen.add(key)
            edges.append({
                "src": src,
                "dst": dst,
                "kind": kind,
                "confidence": 0.65,
            })

    edges.sort(
        key=lambda e: (
            str(e.get("kind", "")),
            str(e.get("src", "")),
            str(e.get("dst", "")),
        )
    )

    return {
        "schema_version": "edges@v1",
        "language": "kotlin",
        "edges": edges,
        "meta": {
            "total": len(edges),
            "internal": sum(
                1
                for e in edges
                if str(e.get("kind", "")).startswith("kotlin:")
            ),
            "import_internal": sum(
                1
                for e in edges
                if e.get("kind") == "kotlin:import:internal"
            ),
            "symbol_internal": sum(
                1
                for e in edges
                if e.get("kind") == "kotlin:symbol:internal"
            ),
            "kotlin_node_id_space": node_id_space,
            "external_included": False,
        },
    }