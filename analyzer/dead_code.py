# analyzer/dead_code.py
from __future__ import annotations
import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Set, Tuple, Optional, Iterable

from analyzer.module_resolve import absolutize_module, drop_init_suffix
from analyzer.ast_common import ModuleIndexCollector, collect_class_usages
from adapters.canonical import to_posix, canon_file_id

# ----------------------- dataclasses -----------------------

@dataclass
class SymbolUse:
    module: str
    name: str             # simple name, e.g. "foo"; for methods store "Cls.foo" if you wish
    kind: str             # "func" | "class" | "var" | "import"
    lineno: int
    col: int


@dataclass
class ModuleReport:
    module: str
    path: str
    # negative findings
    unused_functions: List[SymbolUse] = field(default_factory=list)
    unused_classes: List[SymbolUse] = field(default_factory=list)
    unused_vars: List[SymbolUse] = field(default_factory=list)        # top-level assigned constants only
    unused_imports: List[SymbolUse] = field(default_factory=list)
    # module-level
    is_potentially_dead: bool = False         # nobody imports/references the module (subject to roots)
    notes: List[str] = field(default_factory=list)


@dataclass
class DeadCodeReport:
    modules: Dict[str, ModuleReport] = field(default_factory=dict)
    # quick summaries
    dead_modules: List[str] = field(default_factory=list)


# ----------------------- helpers -----------------------

def _println(*parts):
    """Unconditional diagnostics (no env gate)."""
    try:
        print(*parts)
    except Exception:
        pass


def _normalize_runtime_module_ids(runtime_modules: Optional[Iterable[str]]) -> set[str]:
    out: set[str] = set()
    if not runtime_modules:
        return out
    for name in runtime_modules:
        if not name:
            continue
        s = to_posix(str(name)).strip(".")
        if not s:
            continue
        if s.endswith(".__init__"):
            s = s[: -len(".__init__")]
        out.add(s)
    return out


def _node_aliases(
    nodes: Dict[str, Dict],
    *,
    scan_root: Optional[str | Path] = None,
) -> Tuple[Dict[str, Set[str]], Dict[str, str]]:
    """
    For each primary node key (mid), collect all forms it might appear as:
      - primary key itself
      - absolute/relative path-ish forms (to_posix(path))
      - package path without '__init__.py'
      - dotted id via adapters.canonical.canon_module_id(path, None) if available
      - dotted without '.__init__'
      - repo-relative file IDs via canon_file_id(..., scan_root, prefer_pkg_init=*)
    Returns (aliases_for, alias_to_primary) maps.
    """
    aliases_for: Dict[str, Set[str]] = {}
    alias_to_primary: Dict[str, str] = {}

    for mid, meta in (nodes or {}).items():
        al: Set[str] = set([mid])
        path = str((meta or {}).get("path") or "")
        if path:
            p = to_posix(path)
            al.add(p)
            if p.endswith("/__init__.py"):
                al.add(p[: -len("/__init__.py")])
            # dotted guesses (best-effort; scan_root-independent)
            try:
                from adapters.canonical import canon_module_id as _canon_mod
                dotted = _canon_mod(path, None)
                if dotted:
                    al.add(dotted)
                    if dotted.endswith(".__init__"):
                        al.add(dotted[: -len(".__init__")])
            except Exception:
                pass
            # repo-relative file ids (root-aware)
            try:
                fp_rel = canon_file_id(path, scan_root, prefer_pkg_init=False)
                if fp_rel:
                    al.add(fp_rel)
                pkg_rel = canon_file_id(path, scan_root, prefer_pkg_init=True)
                if pkg_rel:
                    al.add(pkg_rel)
            except Exception:
                pass

        aliases_for[mid] = al
        for a in al:
            prev = alias_to_primary.get(a)
            if prev is None:
                alias_to_primary[a] = mid
            elif prev != mid:
                # Non-fatal: surface potential collisions (e.g., two geometry.py files)
                _println("[DEAD:ALIAS-COLLISION]", a, "->", prev, "(existing),", "ignoring", mid)

    return aliases_for, alias_to_primary


def _resolve_abs(path: str, scan_root: Optional[str | Path]) -> Path:
    """Resolve file path against scan_root (if given) to ensure stable reads."""
    p = Path(path)
    if p.is_absolute() or not scan_root:
        return p
    return Path(scan_root) / p


def _fs_path_for_node(
    mid: str,
    meta: Dict,
    *,
    scan_root: Optional[str | Path],
) -> Path:
    """
    Best-effort mapping from a node's meta["path"]/id to a real filesystem path.

    Priority:
      1) Interpret meta["path"] as a repo-relative file-id via canon_file_id()
      2) Fall back to treating meta["path"] as a literal path (absolute or relative)
    """
    raw = str((meta or {}).get("path") or "").strip()
    sr = str(scan_root) if scan_root is not None else None

    # Try file-id interpretation first (root-aware)
    if sr and raw:
        try:
            fid = canon_file_id(raw, sr, prefer_pkg_init=False, strict_suffix=False)
        except Exception:
            fid = None
        if not fid:
            try:
                fid = canon_file_id(raw, sr, prefer_pkg_init=True, strict_suffix=False)
            except Exception:
                fid = None
        if fid:
            return Path(sr) / fid

    # Fallback: treat as a direct path
    if raw:
        return _resolve_abs(raw, scan_root)

    # Last resort: try mid itself as a path-ish thing
    return _resolve_abs(mid, scan_root)

def _collect_from_imports_by_provider(
    nodes: Dict[str, Dict],
    debug_symbol: Optional[Tuple[str, str]] = None,
    *,
    scan_root: Optional[str | Path] = None,
) -> Dict[str, Set[str]]:
    """
    Build provider -> {imported/used symbol names} across the project.

    Credits providers for:
      • from X import name                 (absolute or relative; existing behavior)
      • import X as alias; alias.name(...) (attribute use via alias)

    Handles absolute/relative resolution and credits under any key shape present
    in `nodes` (dotted, 'path.py', 'pkg/__init__.py', repo-relative file ids).
    """
    dbg_mod, dbg_name = debug_symbol if debug_symbol else (None, None)

    def _log(*parts):
        _println("[DEAD:PROV]", *parts)

    by_provider: Dict[str, Set[str]] = {}
    aliases_for, alias_to_primary = _node_aliases(nodes, scan_root=scan_root)

    def _land_keys(dotted: str) -> List[str]:
        """
        Try plausible key shapes for the provider, then map to primary via alias_to_primary.
        IMPORTANT: resolve file ids with scan_root to avoid basename collisions.
        """
        cands: List[str] = [dotted]
        if dotted.endswith(".__init__"):
            cands.append(dotted[:-len(".__init__")])
        try:
            fp = canon_file_id(dotted, scan_root, prefer_pkg_init=False)
            if fp:
                cands.append(fp)
            pkg = canon_file_id(dotted, scan_root, prefer_pkg_init=True)
            if pkg:
                cands.append(pkg)
        except Exception:
            pass
        landed: List[str] = []
        for c in cands:
            primary = alias_to_primary.get(c)
            if primary:
                landed.append(primary)
        return landed

    files_seen = 0
    from_import_credits = 0
    alias_attr_credits = 0
    seen_alias_credit = set()  # (prov_key, sym, importer_mid)
    seen_from_credit = set()   # (prov_key, sym, importer_mid)

    for importer_mid, meta in (nodes or {}).items():
        raw_path = (meta or {}).get("path")
        apath = _fs_path_for_node(importer_mid, meta, scan_root=scan_root)

        try:
            src = apath.read_text(encoding="utf-8")
            tree = ast.parse(src, filename=str(apath))
        except Exception as ex:
            _log(
                "parse-skip",
                importer_mid,
                f"raw_path={raw_path!r}",
                f"abs={apath.as_posix()}",
                f"reason={ex.__class__.__name__}",
            )
            continue

        files_seen += 1

        # If file is __init__.py, treat consumer id as its package; else drop init suffix.
        is_pkg_init = apath.name == "__init__.py"
        consumer = (importer_mid or "") if is_pkg_init else drop_init_suffix(importer_mid or "")

        # pass 1: from-imports + alias map
        alias_map: Dict[str, str] = {}  # local alias -> provider dotted module

        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom):
                raw_names = [getattr(a, "name", None) for a in (n.names or [])]
                names_here: Set[str] = set(a for a in raw_names if isinstance(a, str) and a and a != "*")
                if not names_here:
                    continue

                # Resolve provider dotted name (absolute or relative)
                if (n.level or 0) == 0:
                    mod_token = (n.module or "").strip(".")
                else:
                    dotted = ("." * (n.level or 0)) + (n.module or "")
                    mod_token = absolutize_module(dotted, consumer) or ""

                if not mod_token:
                    continue

                # Create alias bindings for submodule imports to catch attr usage later.
                for a in (n.names or []):
                    nm = getattr(a, "name", None)
                    if not isinstance(nm, str) or not nm or nm == "*":
                        continue
                    alias = getattr(a, "asname", None) or nm
                    alias_map[alias] = f"{mod_token}.{nm}"

                keys = _land_keys(mod_token)
                for key in keys:
                    for nm in names_here:
                        tup = (key, nm, importer_mid)
                        if tup in seen_from_credit:
                            continue
                        seen_from_credit.add(tup)
                        by_provider.setdefault(key, set()).add(nm)
                        from_import_credits += 1
                        _log("from-credit", f"prov={key}", f"names={[nm]}", f"by={importer_mid}")

            elif isinstance(n, ast.Import):
                # Only explicit aliases:
                #   import ui.window.init_blocks as init_blocks
                for a in n.names or []:
                    prov = (a.name or "").strip(".")
                    al = a.asname
                    if prov and al:
                        alias_map[al] = prov

        # pass 2: alias.attr uses -> credit symbol to provider
        if alias_map:
            used_by_provider: Dict[str, Set[str]] = {}

            class _AttrTracer(ast.NodeVisitor):
                def visit_Attribute(self, node: ast.Attribute) -> None:
                    try:
                        base = node.value
                        if isinstance(base, ast.Name) and base.id in alias_map:
                            prov = alias_map[base.id]  # dotted provider (may be submodule)
                            sym = node.attr
                            if sym:
                                used_by_provider.setdefault(prov, set()).add(sym)
                                if dbg_mod == prov and (dbg_name is None or dbg_name == sym):
                                    _log("alias-attr-hit", f"prov={prov}", f"sym={sym}", f"in={importer_mid}")
                    except Exception:
                        pass
                    self.generic_visit(node)

            _AttrTracer().visit(tree)

            for prov, names_here in used_by_provider.items():
                keys = _land_keys(prov)
                for key in keys:
                    for nm in names_here:
                        tup = (key, nm, importer_mid)
                        if tup in seen_alias_credit:
                            continue
                        seen_alias_credit.add(tup)
                        by_provider.setdefault(key, set()).add(nm)
                        alias_attr_credits += 1
                        _log("alias-credit", f"prov={key}", f"names={[nm]}", f"by={importer_mid}")

    _log(
        "summary",
        f"files={files_seen}",
        f"providers={len(by_provider)}",
        f"from_credits={from_import_credits}",
        f"alias_credits={alias_attr_credits}",
    )
    return by_provider

# ----------------------- public API -----------------------
def analyze_dead_code(
    *,
    graph: Dict,
    seeds: Optional[Iterable[str]] = None,
    extra_roots: Optional[Iterable[str]] = None,
    parsed_universe: Optional[Iterable[str]] = None,
    runtime_modules: Optional[Iterable[str]] = None,
    debug_symbol: Optional[Tuple[str, str]] = None,
    scan_root: Optional[str | Path] = None,
) -> DeadCodeReport:
    # Graph sanity / shapes
    try:
        ntype = type(graph.get("nodes", None)) if isinstance(graph, dict) else type(graph)
        _println("[DEAD] graph type:", type(graph).__name__, "nodes type:", getattr(ntype, "__name__", str(ntype)))
    except Exception:
        _println("[DEAD] graph type: <uninspectable>")

    if not isinstance(graph, dict) or "nodes" not in graph:
        raise TypeError("dead_code: expected graph as dict with keys {'nodes','edges'}")

    nodes_raw = graph.get("nodes", {}) or {}
    edges_raw = graph.get("edges", []) or []

    if not isinstance(nodes_raw, dict):
        raise TypeError(f"dead_code: graph['nodes'] must be a dict; got {type(nodes_raw).__name__}")
    if not isinstance(edges_raw, (list, tuple)):
        raise TypeError(f"dead_code: graph['edges'] must be a list; got {type(edges_raw).__name__}")

    # Normalize edges to dicts
    nodes: Dict[str, Dict] = nodes_raw
    edges: List[dict] = []
    bad_edge_shapes = 0
    for e in edges_raw:
        if isinstance(e, dict):
            edges.append(e)
        elif isinstance(e, (list, tuple)) and len(e) >= 2:
            edges.append({"src": e[0], "dst": e[1]})
        else:
            bad_edge_shapes += 1

    _println("[DEAD] nodes:", len(nodes), "edges:", len(edges))
    if bad_edge_shapes:
        _println("[DEAD] edges remapped:", len(edges), "bad_edge_shapes:", bad_edge_shapes)

    dbg_mod, dbg_name = debug_symbol if debug_symbol else (None, None)

    # Universe / runtime sets
    parsed_ids: Set[str] = set(parsed_universe) if parsed_universe else set(nodes.keys())
    runtime_ids: Set[str] = _normalize_runtime_module_ids(runtime_modules)
    _println("[DEAD] universe:", len(parsed_ids), "runtime_ids:", len(runtime_ids))

    # Build import graph
    imports_of: Dict[str, Set[str]] = {}
    imported_by: Dict[str, Set[str]] = {}

    for e in edges:
        u = str(e.get("src") or e.get("source") or e.get("from") or "")
        v = str(e.get("dst") or e.get("target") or e.get("to") or "")
        if not u or not v or u == v:
            continue
        imports_of.setdefault(u, set()).add(v)
        imported_by.setdefault(v, set()).add(u)

    _println("[DEAD] import-graph (primary):", "imports_of:", len(imports_of), "imported_by:", len(imported_by))

    # Roots
    default_roots = set()
    if seeds:
        default_roots |= set(seeds)

    for mid, meta in nodes.items():
        path = str((meta or {}).get("path") or "")
        base = Path(path).name
        if base in ("main.py", "__main__.py"):
            default_roots.add(mid)

    if extra_roots:
        default_roots |= set(extra_roots)

    if not default_roots:
        default_roots = set(parsed_ids)

    _println("[DEAD] roots (primary):", len(default_roots))

    # Reachability
    live_modules: Set[str] = set()

    def dfs(m: str) -> None:
        if m in live_modules:
            return
        live_modules.add(m)
        for n in imports_of.get(m, ()):
            dfs(n)

    for r in default_roots:
        if r in nodes:
            dfs(r)

    _println("[DEAD] live_modules:", len(live_modules))

    # Imported symbols by provider (from-imports + alias.attr), path-resolved
    imported_symbols_by_provider = _collect_from_imports_by_provider(
        nodes,
        debug_symbol=debug_symbol,
        scan_root=scan_root,
    )

    # Build alias maps once and helpers (root-aware now)
    aliases_for, alias_to_primary = _node_aliases(nodes, scan_root=scan_root)

    if parsed_universe:
        raw_universe = set(parsed_universe)
        norm_universe: Set[str] = set()
        misses: List[str] = []
        for u in raw_universe:
            # map through alias_to_primary if available
            primary = alias_to_primary.get(u)
            if primary:
                norm_universe.add(primary)
            else:
                # try repo-relative file ids first (root-aware), then path-ish/dotted fallbacks
                hit = None
                try:
                    fp = canon_file_id(u, scan_root, prefer_pkg_init=False)
                    if fp:
                        hit = alias_to_primary.get(fp)
                    if not hit:
                        pkg = canon_file_id(u, scan_root, prefer_pkg_init=True)
                        if pkg:
                            hit = alias_to_primary.get(pkg)
                except Exception:
                    pass
                if not hit:
                    up = to_posix(u)
                    u2 = u.removesuffix(".__init__")  # py3.9+
                    hit = alias_to_primary.get(up) or alias_to_primary.get(u2)
                if hit:
                    norm_universe.add(hit)
                else:
                    misses.append(u)
        # if normalization produced nothing but we had a universe, fall back to all nodes
        if not norm_universe:
            _println("[DEAD] universe-normalize produced empty set; falling back to all nodes")
            parsed_ids = set(nodes.keys())
        else:
            parsed_ids = norm_universe
        _println("[DEAD] universe-normalize:",
                 f"in={len(raw_universe)} -> normalized={len(parsed_ids)}",
                 f"misses={len(misses)}")
        if misses:
            _println("[DEAD] universe-misses sample:", sorted(misses)[:10])

    def _imported_from_here_for(mid: str) -> Set[str]:
        names: Set[str] = set()
        for a in aliases_for.get(mid, {mid}):
            s = imported_symbols_by_provider.get(a)
            if s:
                names |= s
        return names

    # Per-module AST analysis
    report = DeadCodeReport()

    files_parsed = 0
    totals = {"unused_imports": 0, "unused_funcs": 0, "unused_classes": 0, "unused_vars": 0}
    dbg_hits = 0

    for mid, meta in nodes.items():
        if parsed_ids and mid not in parsed_ids:
            continue

        apath = _fs_path_for_node(mid, meta, scan_root=scan_root)
        rep = ModuleReport(module=mid, path=str(apath))
        report.modules[mid] = rep

        try:
            text = apath.read_text(encoding="utf-8")
        except Exception as ex:
            rep.notes.append(f"unreadable ({ex.__class__.__name__})")
            _println(
                "[DEAD] file-skip", mid,
                "unreadable:", ex.__class__.__name__,
                "raw_path=", (meta or {}).get("path"),
                "abs=", apath.as_posix(),
                "exists=", apath.exists(),
                "is_file=", apath.is_file(),
            )
            continue

        try:
            tree = ast.parse(text, filename=str(apath))
        except SyntaxError as ex:
            rep.notes.append(f"parse error: {ex}")
            _println("[DEAD] file-parse-error", mid, str(ex))
            continue

        files_parsed += 1

        coll = ModuleIndexCollector()
        coll.visit(tree)
        class_uses = collect_class_usages(tree)
        coll.refs |= class_uses

        # module reachability flag
        if (
            (mid in parsed_ids)
            and (mid not in live_modules)
            and (not imported_by.get(mid))
            and (mid not in runtime_ids)
        ):
            rep.is_potentially_dead = True

        _println(
            "[DEAD] mod", mid,
            f"exports={len(coll.exports)} refs={len(coll.refs)}",
            f"defs(func={len(coll.def_funcs)} class={len(coll.def_classes)} var={len(coll.def_vars)})",
            f"imports={len(coll.imports)}",
            f"dead_module={rep.is_potentially_dead}"
        )

        # Unused imports
        for local, (ln, col) in coll.imports.items():
            if local not in coll.refs:
                rep.unused_imports.append(SymbolUse(mid, local, "import", ln, col))
        totals["unused_imports"] += len(rep.unused_imports)

        live_names = set(coll.refs) | set(coll.exports)
        imported_from_here = _imported_from_here_for(mid)

        # funcs
        for name, (ln, col) in coll.def_funcs.items():
            if mid == dbg_mod and name == dbg_name:
                dbg_hits += 1
                reasons = []
                if name in imported_from_here: reasons.append("imported-from-elsewhere")
                if name in live_names:         reasons.append("local-ref-or-export")
                _println("[DEAD] dbg-func", mid, name, "DECISION=", "KEEP" if reasons else "FLAG", "reasons=", reasons)
            if name in imported_from_here:
                continue
            if name not in live_names:
                rep.unused_functions.append(SymbolUse(mid, name, "func", ln, col))

        # classes
        for name, (ln, col) in coll.def_classes.items():
            if mid == dbg_mod and name == dbg_name:
                dbg_hits += 1
                reasons = []
                if name in imported_from_here: reasons.append("imported-from-elsewhere")
                if name in live_names:         reasons.append("local-ref-or-export")
                _println("[DEAD] dbg-class", mid, name, "DECISION=", "KEEP" if reasons else "FLAG", "reasons=", reasons)
            if name in imported_from_here:
                continue
            if name not in live_names:
                rep.unused_classes.append(SymbolUse(mid, name, "class", ln, col))

        # vars
        for name, (ln, col) in coll.def_vars.items():
            if name.startswith("_"):
                continue
            if mid == dbg_mod and name == dbg_name:
                dbg_hits += 1
                reasons = []
                if name in live_names:         reasons.append("local-ref-or-export")
                if name in imported_from_here: reasons.append("imported-from-elsewhere")
                _println("[DEAD] dbg-var", mid, name, "DECISION=", "KEEP" if reasons else "FLAG", "reasons=", reasons)
            if name in imported_from_here:
                continue
            if name not in live_names:
                rep.unused_vars.append(SymbolUse(mid, name, "var", ln, col))

        totals["unused_funcs"]   += len(rep.unused_functions)
        totals["unused_classes"] += len(rep.unused_classes)
        totals["unused_vars"]    += len(rep.unused_vars)

    _println("[DEAD] before-L2 modules:", len(report.modules))
    # L2 suppression (best-effort)
    try:
        from analyzer.l2_method_usage import attach_l2_used_methods
        attach_l2_used_methods(report)
    except Exception as ex:
        _println("[DEAD] l2-skip", f"reason={ex.__class__.__name__}")
    _println("[DEAD] after-L2 modules:", len(report.modules))

    # Summary
    report.dead_modules = sorted([m for m, r in report.modules.items() if r.is_potentially_dead])
    _println(
        "[DEAD] summary",
        f"files_parsed={files_parsed}",
        f"dead_modules={len(report.dead_modules)}",
        f"unused: imports={totals['unused_imports']} funcs={totals['unused_funcs']} "
        f"classes={totals['unused_classes']} vars={totals['unused_vars']}",
        f"dbg_hits={dbg_hits}"
    )
    return report
