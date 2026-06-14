from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from analyzer.ruby.parse_ruby.models import RubyAnalysis
from analyzer.ruby.ruby_call_index import build_call_edges, build_method_fq_to_file
from analyzer.ruby.ruby_folder_index import RubyIndexTables
from analyzer.ruby.ruby_nodefacts_symbols import NodeFactsSymbols, parse_symbols_for_nodefacts
from analyzer.ruby.ruby_rails_index import build_rails_annotations, build_rails_edges
from analyzer_store.types import FileEntry, FolderIndex, NODEFACTS_SCHEMA

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _stable_unique_strs(vals: Iterable[Any]) -> Tuple[str, ...]:
    out: Set[str] = set()
    for v in vals or ():
        try:
            s = str(v).strip()
        except Exception:
            continue
        if s:
            out.add(s)
    return tuple(sorted(out))

def _json_safe(obj: Any) -> Any:
    """
    Convert Ruby parser model objects such as RubyLoc into JSON-safe values.
    Keep this local so nodefacts/edges do not leak parser objects into JSON.
    """
    if obj is None:
        return None

    if isinstance(obj, (str, int, float, bool)):
        return obj

    if isinstance(obj, dict):
        return {
            str(_json_safe(k)): _json_safe(v)
            for k, v in obj.items()
        }

    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_json_safe(v) for v in obj]

    d = getattr(obj, "__dict__", None)
    if isinstance(d, dict):
        return {
            str(k): _json_safe(v)
            for k, v in d.items()
            if not str(k).startswith("_")
        }

    return str(obj)

def _norm_parse_status(s: Any) -> str:
    ss = str(s or "").strip().lower()
    if not ss:
        return "ok"
    if "error" in ss or "fail" in ss or "exception" in ss or ss in {"err"}:
        return "error"
    if "warn" in ss or ss in {"partial", "degraded"}:
        return "warn"
    if ss in {"ok", "success"}:
        return "ok"
    return "warn"


def _upgrade_parse_status(cur: Any, new: Any) -> str:
    cur_n = _norm_parse_status(cur)
    new_n = _norm_parse_status(new)
    rank = {"ok": 0, "warn": 1, "error": 2}
    return new_n if rank.get(new_n, 0) > rank.get(cur_n, 0) else cur_n


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None:
            return default
        return int(v)
    except Exception:
        return default


def _ruby_name_from_id(node_id: str) -> str:
    s = str(node_id or "").strip()
    if not s:
        return ""
    if "/" in s:
        return s.rsplit("/", 1)[-1]
    if "::" in s:
        return s.rsplit("::", 1)[-1]
    return s


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


def _stub_node(node_id: str) -> Dict[str, Any]:
    nid = str(node_id or "").strip()
    return {
        "id": nid,
        "name": _ruby_name_from_id(nid) or nid,
        "language": "ruby",
        "lang": "ruby",
        "imports": (),
        "imports_all_raw": (),
        "imports_external": (),
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
        "language_facts": {},
    }


def _cache_get_for_file(
    *,
    rel: str,
    parse_cache: Any,
) -> Optional[object]:
    if not isinstance(parse_cache, dict) or not parse_cache:
        return None

    rel_s = str(rel or "").replace("\\", "/").strip("/")
    if rel_s in parse_cache:
        return parse_cache[rel_s]

    # Transitional fallback for callers that still key with variants.
    for k in (rel, rel_s):
        try:
            if k and k in parse_cache:
                return parse_cache[k]
        except Exception:
            continue

    return None


def _method_records_for_file(analysis: RubyAnalysis, rel: str) -> List[Dict[str, Any]]:
    pf = (analysis.files or {}).get(rel)
    if pf is None:
        return []

    out: List[Dict[str, Any]] = []

    for meth in getattr(pf, "methods", None) or ():
        rec: Dict[str, Any] = {}

        for attr in ("name", "fq_name", "owner", "visibility", "kind"):
            try:
                val = getattr(meth, attr)
                if val is not None:
                    rec[attr] = val
            except Exception:
                pass

        try:
            loc = getattr(meth, "loc", None)
            if loc is not None:
                rec["loc"] = _json_safe(loc)
        except Exception:
            pass

        if rec:
            out.append(rec)

    return out


def _declaration_records_for_file(analysis: RubyAnalysis, rel: str) -> List[Dict[str, Any]]:
    pf = (analysis.files or {}).get(rel)
    if pf is None:
        return []

    out: List[Dict[str, Any]] = []

    for decl in getattr(pf, "declarations", None) or ():
        rec: Dict[str, Any] = {}

        for attr in ("name", "fq_name", "owner", "kind"):
            try:
                val = getattr(decl, attr)
                if val is not None:
                    rec[attr] = val
            except Exception:
                pass

        try:
            loc = getattr(decl, "loc", None)
            if loc is not None:
                rec["loc"] = _json_safe(loc)
        except Exception:
            pass

        if rec:
            out.append(rec)

    return out


def _reference_records_for_file(analysis: RubyAnalysis, rel: str) -> List[Dict[str, Any]]:
    pf = (analysis.files or {}).get(rel)
    if pf is None:
        return []

    out: List[Dict[str, Any]] = []

    for ref in getattr(pf, "references", None) or ():
        rec: Dict[str, Any] = {}

        for attr in ("name", "fq_name", "owner", "kind"):
            try:
                val = getattr(ref, attr)
                if val is not None:
                    rec[attr] = val
            except Exception:
                pass

        try:
            loc = getattr(ref, "loc", None)
            if loc is not None:
                rec["loc"] = _json_safe(loc)
        except Exception:
            pass

        if rec:
            out.append(rec)

    return out


def _dynamic_requires_for_file(tables: RubyIndexTables, rel: str) -> List[Dict[str, Any]]:
    vals = getattr(tables, "dynamic_requires_by_file", {}).get(rel, [])
    return [dict(v) for v in vals if isinstance(v, dict)]

def _external_specs_for_file(
    *,
    rel: str,
    entry: FileEntry,
    tables: RubyIndexTables,
) -> Tuple[str, ...]:
    """
    Correct external classification.

    Prefer FileEntry.imports_external because the folder-index resolver is the
    authoritative source after provider ranking/filtering.

    Fallback to older table-based reconstruction for transitional artifacts.
    """
    direct = getattr(entry, "imports_external", None)
    if direct:
        return _stable_unique_strs(direct)

    file_to_specs = getattr(tables, "file_to_require_specs", {}) or {}
    provider_index = getattr(tables, "require_spec_to_provider_files", {}) or {}

    specs = file_to_specs.get(rel)
    if not specs:
        specs = list(getattr(entry, "imports_all", ()) or ())

    out: List[str] = []
    for spec0 in specs or ():
        spec = str(spec0 or "").strip()
        if not spec:
            continue

        providers = provider_index.get(spec, []) or []
        if not providers:
            out.append(spec)

    return _stable_unique_strs(out)

def _internal_specs_for_file(
    *,
    rel: str,
    entry: FileEntry,
    tables: RubyIndexTables,
) -> Tuple[str, ...]:
    """
    Return require specs that resolved internally.

    This is useful for nodefacts because imports_internal is target files, while
    users often also need to see which require strings caused those internal deps.
    """
    file_to_specs = getattr(tables, "file_to_require_specs", {}) or {}
    provider_index = getattr(tables, "require_spec_to_provider_files", {}) or {}

    specs = file_to_specs.get(rel)
    if not specs:
        specs = list(getattr(entry, "imports_all", ()) or ())

    out: List[str] = []
    for spec0 in specs or ():
        spec = str(spec0 or "").strip()
        if not spec:
            continue
        providers = provider_index.get(spec, []) or []
        if providers:
            out.append(spec)

    return _stable_unique_strs(out)

def _internal_specs_by_target_for_file(
    *,
    rel: str,
    entry: FileEntry,
    tables: RubyIndexTables,
) -> Dict[str, Tuple[str, ...]]:
    """
    Return target file -> require specs that resolved to that target.

    Edges are graph relationships, but the require string is the explanation.
    This lets import edges carry label/spec metadata such as "sinatra/base".
    """
    file_to_specs = getattr(tables, "file_to_require_specs", {}) or {}
    provider_index = getattr(tables, "require_spec_to_provider_files", {}) or {}

    specs = file_to_specs.get(rel)
    if not specs:
        specs = list(getattr(entry, "imports_all", ()) or ())

    out: Dict[str, List[str]] = {}

    known_targets = {
        str(dst or "").strip()
        for dst in getattr(entry, "imports_internal", ()) or ()
        if str(dst or "").strip()
    }

    for spec0 in specs or ():
        spec = str(spec0 or "").strip()
        if not spec:
            continue

        providers = provider_index.get(spec, []) or []
        for dst0 in providers:
            dst = str(dst0 or "").replace("\\", "/").strip("/")
            if not dst:
                continue

            # Only attach labels to edges that actually survived folder-index
            # filtering/ranking.
            if known_targets and dst not in known_targets:
                continue

            out.setdefault(dst, []).append(spec)

    return {
        dst: _stable_unique_strs(specs_for_dst)
        for dst, specs_for_dst in sorted(out.items())
    }

# ---------------------------------------------------------------------------
# Nodefacts
# ---------------------------------------------------------------------------

def build_nodefacts_from_folder_index(
    *,
    idx: FolderIndex,
    tables: RubyIndexTables,
    analysis: RubyAnalysis,
    cfg: Any = None,
) -> Dict[str, Any]:
    """
        Build a nodefacts@v1.9-compatible dict from a canonical FolderIndex + RubyIndexTables.

    Graph-facing imports:
      - node["imports"] comes from FileEntry.imports_internal.
      - node["imports_all_raw"] preserves raw require specs.

    Ruby-specific facts:
      - declarations/methods/references are additive under node["ruby"].
      - Rails annotations are additive.
    """
    files = getattr(idx, "files", {}) or {}
    fe_list: List[Tuple[str, FileEntry]] = [
        (rel, fe)
        for rel, fe in files.items()
        if isinstance(fe, FileEntry)
    ]
    fe_list.sort(key=lambda kv: str(kv[0]))

    parse_cache = getattr(cfg, "ruby_parse_cache", None) if cfg is not None else None

    nodes: Dict[str, Dict[str, Any]] = {}
    deps_by_node: Dict[str, Set[str]] = {}
    importers_count: Dict[str, int] = {}

    parse_ok_files = 0
    parse_warn_files = 0
    parse_err_files = 0

    loc_total_all = 0
    sloc_total_all = 0
    comment_lines_total_all = 0
    blank_lines_total_all = 0

    # Per-file Rails annotations.
    rails_annotations: Dict[str, list] = {}
    for ann in build_rails_annotations(analysis=analysis):
        rails_annotations.setdefault(ann.file, []).append(
            {
                "type": ann.annotation_type,
                "value": ann.value,
            }
        )

    # Build deps/importers first.
    known_files = set(files.keys())

    for rel, entry in fe_list:
        deps = deps_by_node.setdefault(rel, set())

        for dst0 in getattr(entry, "imports_internal", None) or ():
            dst = str(dst0 or "").strip()
            if not dst or dst == rel:
                continue
            if dst not in known_files:
                continue
            deps.add(dst)
            importers_count[dst] = importers_count.get(dst, 0) + 1

    scc_id_by_node, scc_size_by_id = _compute_scc_from_deps(
        known_files,
        deps_by_node,
    )

    # Build nodes.
    for rel, entry in fe_list:
        cached = _cache_get_for_file(rel=rel, parse_cache=parse_cache)

        imports_external = _external_specs_for_file(
            rel=rel,
            entry=entry,
            tables=tables,
        )
        imports_internal_specs = _internal_specs_for_file(
            rel=rel,
            entry=entry,
            tables=tables,
        )

        # Prefer the Rust-style nodefacts helper if cache is available.
        syms: Optional[NodeFactsSymbols] = None
        try:
            if cached is not None:
                syms = parse_symbols_for_nodefacts(
                    cached,
                    cfg,
                    imports_external=imports_external,
                )
        except Exception:
            syms = None

        # Fall back to folder-index symbols.
        if syms is None:
            syms = tables.symbols_by_file.get(rel)

        status = _upgrade_parse_status(
            getattr(entry, "parse_status", "ok"),
            getattr(syms, "parse_status", "ok") if syms is not None else "ok",
        )

        if status == "ok":
            parse_ok_files += 1
        elif status == "warn":
            parse_warn_files += 1
        else:
            parse_err_files += 1

        fe_loc = _safe_int(getattr(entry, "loc", None))
        fe_sloc = _safe_int(getattr(entry, "sloc", None))
        fe_comment_lines = _safe_int(getattr(entry, "comment_lines", None))
        fe_blank_lines = _safe_int(getattr(entry, "blank_lines", None))

        loc_total_all += fe_loc
        sloc_total_all += fe_sloc
        comment_lines_total_all += fe_comment_lines
        blank_lines_total_all += fe_blank_lines

        deps = deps_by_node.get(rel, set())
        sid = scc_id_by_node.get(rel, "")

        node: Dict[str, Any] = {
            "id": rel,
            "name": _ruby_name_from_id(rel),
            "language": "ruby",
            "lang": "ruby",

            # Graph-facing imports: resolved internal dependency targets.
            "imports": tuple(sorted(deps)),

            # Raw require specs from folder index/parser.
            "imports_all_raw": tuple(getattr(entry, "imports_all", ()) or ()),

            # Require classification by spec.
            "imports_internal": imports_internal_specs,
            "imports_external": imports_external,

            # Back-compat alias for older Ruby output.
            "internal_requires": tuple(sorted(deps)),

            "importers_count": importers_count.get(rel, 0),
            "dependencies_count": len(deps),

            "scc_id": sid,
            "scc_size": scc_size_by_id.get(sid, 1),

            "gen_seq": 0,
            "file": rel,
            "hash": getattr(entry, "hash", None),

            # Metrics from FileEntry.
            "loc": getattr(entry, "loc", None),
            "sloc": getattr(entry, "sloc", None),
            "comment_lines": getattr(entry, "comment_lines", None),
            "blank_lines": getattr(entry, "blank_lines", None),
            "comment_pct": getattr(entry, "comment_pct", None),
            "file_count": 1,
            "size_bytes": getattr(entry, "size_bytes", None),
            "mtime": getattr(entry, "mtime", None),
            "du": None,
            "dd": None,

            "parse_status": status,
            "rails_role": getattr(syms, "rails_role", None) if syms is not None else tables.rails_role_by_file.get(rel, "unknown"),

            # NodeFacts compatibility fields.
            "exports": tuple(getattr(syms, "public_exports", ()) or ()) if syms is not None else (),
            "classes": tuple(getattr(syms, "classes", ()) or ()) if syms is not None else (),
            "functions": tuple(getattr(syms, "top_level_methods", ()) or ()) if syms is not None else (),
            "globals": tuple(getattr(syms, "constants", ()) or ()) if syms is not None else (),

            # Ruby semantic fields.
            "package": getattr(syms, "package", None) if syms is not None else None,
            "declared_types": tuple(getattr(syms, "declared_types", ()) or ()) if syms is not None else (),
            "declared_types_fq": tuple(getattr(syms, "declared_types_fq", ()) or ()) if syms is not None else (),
            "public_exports": tuple(getattr(syms, "public_exports", ()) or ()) if syms is not None else (),
            "modules": tuple(getattr(syms, "modules", ()) or ()) if syms is not None else (),
            "constants": tuple(getattr(syms, "constants", ()) or ()) if syms is not None else (),
            "top_level_methods": tuple(getattr(syms, "top_level_methods", ()) or ()) if syms is not None else (),
            "methods": tuple(getattr(syms, "methods", ()) or ()) if syms is not None else (),
            "methods_fq": tuple(getattr(syms, "methods_fq", ()) or ()) if syms is not None else (),
            "dynamic_requires_count": int(getattr(syms, "dynamic_requires_count", 0) or 0) if syms is not None else 0,
        }

        # Rails DSL annotations.
        anns = rails_annotations.get(rel)
        if anns:
            node["rails_annotations"] = anns

        # Preserve richer Ruby analysis records additively.
        declarations = _declaration_records_for_file(analysis, rel)
        methods = _method_records_for_file(analysis, rel)
        references = _reference_records_for_file(analysis, rel)
        dynamic_requires = _dynamic_requires_for_file(tables, rel)

        ruby_facts: Dict[str, Any] = {}

        # Start with facts already carried by FileEntry.language_facts.
        entry_language_facts = getattr(entry, "language_facts", {}) or {}
        if isinstance(entry_language_facts, dict):
            existing_ruby = entry_language_facts.get("ruby", {})
            if isinstance(existing_ruby, dict):
                ruby_facts.update(existing_ruby)

        if declarations:
            ruby_facts["declarations"] = declarations
        if methods:
            ruby_facts["methods"] = methods
        if references:
            ruby_facts["references"] = references
        if dynamic_requires:
            ruby_facts["dynamic_requires"] = dynamic_requires

        if imports_internal_specs:
            ruby_facts["internal_require_specs"] = list(imports_internal_specs)

        # Include symbols in language_facts for bundle consumers.
        symbols: Dict[str, Any] = {}
        rails_role_val = (
            getattr(syms, "rails_role", None)
            if syms is not None
            else tables.rails_role_by_file.get(rel, "unknown")
        )
        if rails_role_val and rails_role_val != "unknown":
            symbols["rails_role"] = rails_role_val

        if syms is not None:
            for sym_attr in ("declared_types", "declared_types_fq", "modules", "constants", "methods_fq"):
                val = getattr(syms, sym_attr, None)
                if val:
                    symbols[sym_attr] = list(val)

        if symbols:
            ruby_facts["symbols"] = symbols

        if ruby_facts:
            node["language_facts"] = {
                "ruby": _json_safe(ruby_facts),
            }

            # Transitional alias for older consumers. Remove later once the
            # standard/compressed exporters consistently read language_facts.
            node["ruby"] = _json_safe(ruby_facts)

        err = tables.error_by_file.get(rel)
        if err:
            node["error"] = err

        nodes[rel] = {
            k: v
            for k, v in node.items()
            if v is not None and v != [] and v != ()
        }

    # Ensure stubs for referenced nodes, though internal_only filtering above
    # should normally keep all deps in known_files.
    for src, deps in deps_by_node.items():
        if src not in nodes:
            nodes[src] = _stub_node(src)
        for dst in deps:
            if dst not in nodes:
                nodes[dst] = _stub_node(dst)

    comment_pct_total = (comment_lines_total_all / loc_total_all) if loc_total_all else None

    nodes_with_imports_all_raw = sum(
        1 for node in nodes.values()
        if node.get("imports_all_raw")
    )
    raw_import_specs_total = sum(
        len(node.get("imports_all_raw") or ())
        for node in nodes.values()
    )

    external_import_specs_total = sum(
        len(node.get("imports_external") or ())
        for node in nodes.values()
    )

    return {
        "schema_version": NODEFACTS_SCHEMA,
        "language": "ruby",
        "nodes": {k: nodes[k] for k in sorted(nodes.keys())},
        "meta": {
            "module_name": "",
            "nodes": len(nodes),
            "node_count": len(nodes),
            "files_indexed": len(files),
            "parse_ok_files": parse_ok_files,
            "parse_warn_files": parse_warn_files,
            "parse_err_files": parse_err_files,
            "loc_total": loc_total_all,
            "sloc_total": sloc_total_all,
            "comment_lines_total": comment_lines_total_all,
            "blank_lines_total": blank_lines_total_all,
            "comment_pct": comment_pct_total,
            "rails_detected": idx.meta.get("rails_detected", "false"),
            "used_parse_cache": bool(isinstance(parse_cache, dict) and parse_cache),
            "nodes_with_imports_all_raw": int(nodes_with_imports_all_raw),
            "raw_import_specs_total": int(raw_import_specs_total),
            "external_import_specs_total": int(external_import_specs_total),
        },
    }


# ---------------------------------------------------------------------------
# Edges
# ---------------------------------------------------------------------------

def build_edges_from_folder_index(
    *,
    idx: FolderIndex,
    tables: RubyIndexTables,
    analysis: RubyAnalysis,
    cfg: Any = None,
    internal_only: bool = True,
) -> Dict[str, Any]:
    """
    Build an edges@v1 dict from a canonical FolderIndex + RubyIndexTables.

    Require edges come from FileEntry.imports_internal.
    Call and Rails association edges come from the Ruby analysis indexes.
    """
    min_call_confidence: float = float(
        getattr(cfg, "call_edge_min_confidence", 0.6)
    ) if cfg is not None else 0.6

    include_call_edges: bool = bool(
        getattr(
            cfg,
            "ruby_include_method_call_edges",
            getattr(cfg, "ruby_include_candidate_edges", True),
        )
    ) if cfg is not None else True

    include_rails_assoc: bool = bool(
        getattr(cfg, "include_rails_association_edges", True)
    ) if cfg is not None else True

    include_external_edges: bool = bool(
        getattr(cfg, "ruby_include_external_edges", False)
    ) if cfg is not None else False

    edges: List[Dict[str, Any]] = []
    known_files: Set[str] = set(idx.files.keys())

    def _add_edge(
        *,
        src: str,
        dst: str,
        kind: str,
        weight: float,
        label: Optional[str] = None,
        spec: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> None:
        if not src or not dst or not kind:
            return
        if src == dst:
            return
        if internal_only and dst not in known_files and not dst.startswith("ext::"):
            return

        e: Dict[str, Any] = {
            "src": src,
            "dst": dst,
            "kind": kind,
            "weight": round(float(weight), 3),
            "confidence": round(float(weight), 3),
        }
        if label:
            e["label"] = label
        if spec:
            e["spec"] = spec
        if reason:
            e["reason"] = reason
        edges.append(e)

    # ------------------------------------------------------------------
    # 1. Require edges — from FileEntry.imports_internal
    # ------------------------------------------------------------------
    for rel, entry in idx.files.items():
        specs_by_target = _internal_specs_by_target_for_file(
            rel=rel,
            entry=entry,
            tables=tables,
        )

        for target in getattr(entry, "imports_internal", None) or ():
            target_s = str(target or "").strip()
            if not target_s:
                continue

            specs = specs_by_target.get(target_s, ())
            label = specs[0] if specs else None

            _add_edge(
                src=rel,
                dst=target_s,
                kind="import",
                weight=1.0,
                label=label,
                spec=label,
                reason=(
                    "ruby_require_internal:provider_index"
                    if label
                    else "ruby_require_internal"
                ),
            )

    # ------------------------------------------------------------------
    # 1b. Optional external require edges
    # ------------------------------------------------------------------
    if include_external_edges:
        for rel, entry in idx.files.items():
            external_specs = _external_specs_for_file(
                rel=rel,
                entry=entry,
                tables=tables,
            )
            for spec in external_specs:
                _add_edge(
                    src=rel,
                    dst=f"ext::{spec}",
                    kind="ruby:require:external",
                    weight=0.3,
                    label=spec,
                    reason="ruby_require_external",
                )

    # ------------------------------------------------------------------
    # 2. Call edges — from Ruby CallIndex, confidence-gated
    # ------------------------------------------------------------------
    if include_call_edges:
        method_fq_to_file = build_method_fq_to_file(analysis)

        # Keep declaration-to-file fallback. This can help class/module calls,
        # but method_fq_to_file remains the stronger source for methods.
        for fq, file in tables.fq_decl_to_file.items():
            method_fq_to_file.setdefault(fq, file)

        call_edges = build_call_edges(
            analysis=analysis,
            method_fq_to_file=method_fq_to_file,
            min_confidence=min_call_confidence,
        )

        for ce in call_edges:
            _add_edge(
                src=ce.source_file,
                dst=ce.target_file,
                kind="call",
                weight=float(ce.confidence),
                label=ce.target,
                reason=ce.reason,
            )

    # ------------------------------------------------------------------
    # 3. Rails association edges
    # ------------------------------------------------------------------
    if include_rails_assoc:
        for re in build_rails_edges(
            analysis=analysis,
            fq_decl_to_file=tables.fq_decl_to_file,
        ):
            if not re.target_file:
                continue

            _add_edge(
                src=re.source_file,
                dst=re.target_file,
                kind="rails_association",
                weight=0.9,
                label=f"{re.edge_type}:{re.association_name}",
                reason=re.reason,
            )

    # ------------------------------------------------------------------
    # Dedup same src+dst+kind, keeping highest weight/confidence.
    # ------------------------------------------------------------------
    deduped: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

    for e in edges:
        key = (str(e["src"]), str(e["dst"]), str(e["kind"]))

        if key not in deduped:
            deduped[key] = e
            continue

        cur = deduped[key]
        cur_weight = float(cur.get("weight", 0.0))
        new_weight = float(e.get("weight", 0.0))

        # Prefer the higher-confidence edge. If tied, prefer the one with more
        # explanatory metadata.
        cur_richness = sum(1 for k in ("label", "spec", "reason") if cur.get(k))
        new_richness = sum(1 for k in ("label", "spec", "reason") if e.get(k))

        if new_weight > cur_weight or (
            new_weight == cur_weight and new_richness > cur_richness
        ):
            deduped[key] = e

    final_edges = sorted(
        deduped.values(),
        key=lambda e: (str(e.get("kind", "")), str(e.get("src", "")), str(e.get("dst", ""))),
    )

    by_kind: Dict[str, int] = {}
    for e in final_edges:
        kind = str(e.get("kind", "unknown"))
        by_kind[kind] = by_kind.get(kind, 0) + 1

    return {
        "schema_version": "edges@v1",
        "language": "ruby",
        "edges": final_edges,
        "meta": {
            "total": len(final_edges),
            "internal": sum(1 for e in final_edges if not str(e.get("dst", "")).startswith("ext::")),
            "by_kind": by_kind,
            "call_min_confidence": min_call_confidence,
            "call_edges_included": bool(include_call_edges),
            "rails_association_edges_included": bool(include_rails_assoc),
            "external_edges_included": bool(include_external_edges),
        },
    }