from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path
import os
import re

# Canonical helpers
from adapters.canonical import to_posix, canon_file_id, canon_module_id, normalize_root

# Bridge internals (dedupe/build + token helpers)
from .internals import (
    build_import_edge,
    edge_dedupe_key,
    token_to_module,
)

# Node payload (single source of truth for path/module/imports/defs/annotations)
from .bridge_payloads import node_payload_from  # ← align with adapters/analyzer_bridge/payloads.py

# Provenance tagger (stdlib/third_party/local, etc.)
from adapters.analyzer_bridge.provenance import tag_import_provenance


__all__ = [
    "enrich_import_provenance",      # compat alias
    "infer_edges_from_import",
    "normalize_graph_for_contracts",
]

# ---------------------------------------------------------------------------
# Diagnostics / logging
# ---------------------------------------------------------------------------

try:
    from diagnostics.logging import log_event as _log_event  # type: ignore[attr-defined]

    def _log(evt: str, **fields: Any) -> None:
        """
        Lightweight structured logging via the central logging controller.

        Examples:
            _log("ANALYZER_BRIDGE:infer_edges_begin", edges_in=123)
            _log("ANALYZER_BRIDGE:normalize_end", edges_out=456, caller="...")

        Logging must never affect core behavior.
        """
        try:
            _log_event(evt, **fields)
        except Exception:
            pass

except Exception:  # pragma: no cover
    # Fallback: keep completely silent if diagnostics are unavailable.
    def _log(evt: str, **fields: Any) -> None:  # type: ignore[no-redef]
        return

# ---------------------------------------------------------------------------
# Local-only helpers (kept minimal; no duplication with payloads)
# ---------------------------------------------------------------------------

# Regexes for import row parsing
_FROM_RE = re.compile(r"^\s*from\s+(\.+)?([A-Za-z0-9_.]*)\s+import\s+")
_IMPORT_RE = re.compile(r"^\s*import\s+([A-Za-z0-9_.,\s]+)")


def _parse_import_row(line: str) -> List[str]:
    """Parse 'import a, b as c' -> ['a', 'b'] (aliases ignored)."""
    s = (line or "").strip()
    m = _IMPORT_RE.match(s)
    if not m:
        return []
    mods: List[str] = []
    for tok in m.group(1).split(","):
        tok = tok.strip()
        if not tok:
            continue
        tok = tok.split(" as ", 1)[0].strip()
        if tok and tok != "*":
            mods.append(tok)
    return mods


def _parse_from_row(line: str) -> Tuple[Optional[str], List[Tuple[str, Optional[str]]]]:
    """
    'from pkg.sub import A, B as C' -> (module_token, [(name, alias), ...]).
    module_token may be relative (e.g., '.config').
    """
    s = (line or "").strip()
    m = _FROM_RE.match(s)
    if not m:
        return None, []
    dots, tail = m.groups()
    module_token = (dots or "") + (tail or "")
    try:
        after = s.split(" import ", 1)[1]
    except Exception:
        return module_token or None, []
    pairs: List[Tuple[str, Optional[str]]] = []
    for tok in after.split(","):
        tok = tok.strip()
        if not tok:
            continue
        name = tok.split(" as ", 1)[0].strip()
        alias = tok.split(" as ", 1)[1].strip() if " as " in tok else None
        if name and name != "*":
            pairs.append((name, alias))
    return (module_token or None), pairs


def _resolve_relative_module(src_mod: Optional[str], rel: str) -> Optional[str]:
    """
    Resolve Python relative module tokens ('.', '..', '.foo') against src_mod.
    src_mod is the importer's module id (e.g., 'analyzer' for analyzer/__init__.py).
    """
    if not isinstance(rel, str) or not rel.startswith(".") or not src_mod:
        return None
    # count leading dots
    i = 0
    while i < len(rel) and rel[i] == ".":
        i += 1
    remainder = rel[i:]
    parts = src_mod.split(".")
    up = max(i - 1, 0)
    if up > len(parts):
        return None
    base = parts[: len(parts) - up] if up else parts
    if remainder:
        base.append(remainder)
    mod_abs = ".".join(p for p in base if p)
    return mod_abs or None


def _norm_node_id_to_module(node_id: str) -> Optional[str]:
    """
    Best-effort mapper from NodeId → module id.

    Under the current contracts, NodeId *is already* the module id
    (e.g. 'scrapy.core.engine', 'ui.app.run_gui'), so this is mostly
    for legacy shapes and odd cases.
    """
    if not isinstance(node_id, str) or not node_id:
        return None
    return token_to_module(node_id)


# ---------------------------------------------------------------------------
# Public API (no disk writes; repo_root is scan root for relative display only)
# ---------------------------------------------------------------------------

def infer_edges_from_import(
    graph: Any,
    *,
    repo_root: Optional[Path | str] = None,
) -> Dict[str, Any]:
    """
    Node ids are treated as stable, dotted module ids; we never shorten them to
    'core.engine' style. Resolution from import tokens → NodeId goes through
    module/file alias tables only.
    """
    def _ek(g_like: Any) -> int:
        try:
            return len(g_like.get("edges") or [])
        except Exception:
            return -1

    # Coerce graph → plain dict
    if graph is None:
        g: Dict[str, Any] = {"nodes": {}, "edges": []}
    elif isinstance(graph, dict):
        g = {
            "nodes": dict(graph.get("nodes") or {}),
            "edges": list(graph.get("edges") or []),
        }
    else:
        g = {
            "nodes": dict(getattr(graph, "nodes", {}) or {}),
            "edges": list(getattr(graph, "edges", []) or []),
        }

    _log(
        "ANALYZER_BRIDGE:infer_edges_begin",
        edges_in=_ek(g),
    )

    # Normalize repo_root (string is fine for canon_* helpers)
    root = to_posix(str(repo_root)) if repo_root else None

    nodes_in: Dict[str, Any] = g["nodes"]
    out_nodes: Dict[str, Any] = {}
    imports_rows_by_id: Dict[str, List[str]] = {}

    # 1) Build normalized node payloads (delegated; SSOT)
    for node_id, n in nodes_in.items():
        pid = str(node_id)
        payload = node_payload_from(pid, n, repo_root=root)
        out_nodes[pid] = payload
        imports_rows_by_id[pid] = list(payload.get("imports_rows") or [])

    g["nodes"] = out_nodes

    # 2) Carry existing edges and build dedupe set
    edges: List[Dict[str, Any]] = list(g.get("edges") or [])
    seen: set[Tuple[str, str, str, str, str]] = set()
    existing_pairs: set[tuple[str, str]] = set()
    for e in edges:
        try:
            seen.add(edge_dedupe_key(e))
            u = str(e.get("src") or "")
            v = str(e.get("dst") or "")
            if u and v:
                existing_pairs.add((u, v))
        except Exception:
            pass

    # --- diagnostics snapshot for incoming edges ---
    try:
        uniq_pairs_in = len(
            {(str(e.get("src")), str(e.get("dst"))) for e in edges if e}
        )
    except Exception:
        uniq_pairs_in = -1
    _log(
        "ANALYZER_BRIDGE:infer_edges_carry_in",
        edges=len(edges),
        dedupe_keys=len(seen),
        uniq_pairs=uniq_pairs_in,
    )

    # 3) Build resolvers: module/file → node_id
    module_to_id: Dict[str, str] = {}
    file_to_id: Dict[str, str] = {}

    def _index_mod_aliases(mod: str, nid: str) -> None:
        """
        Index a module id and its dotted prefixes to the same NodeId.

        Example:
          'scrapy.core.engine' →
            'scrapy.core.engine', 'scrapy.core', 'scrapy'
        """
        if not mod:
            return
        module_to_id.setdefault(mod, nid)
        if mod.endswith(".__init__"):
            module_to_id.setdefault(mod[: -len(".__init__")], nid)
        parts = mod.split(".")
        for k in range(len(parts) - 1, 0, -1):
            module_to_id.setdefault(".".join(parts[:k]), nid)
        # Also index path variants for this module
        try:
            f_py = canon_file_id(mod, root, prefer_pkg_init=False, strict_suffix=False)
            f_in = canon_file_id(mod, root, prefer_pkg_init=True, strict_suffix=False)
            for f in (f_py, f_in):
                if isinstance(f, str) and f:
                    file_to_id.setdefault(to_posix(f), nid)
        except Exception:
            pass

    def _index_path_aliases(path_id: str, nid: str) -> None:
        """
        Index repo-relative file ids and their module-ized forms.
        """
        p = to_posix(path_id)
        if not p:
            return
        file_to_id.setdefault(p, nid)
        try:
            m = canon_module_id(p, root)
            if m:
                _index_mod_aliases(m, nid)
        except Exception:
            pass
        if p.endswith("/__init__.py"):
            file_to_id.setdefault(p[: -len("/__init__.py")], nid)

    for nid, payload in out_nodes.items():
        m = str(payload.get("module") or "") if payload else ""
        if m:
            _index_mod_aliases(m, nid)
        p = str(payload.get("path") or "") if payload else ""
        if p:
            _index_path_aliases(p, nid)

    # 4) Provider resolution cache
    _provider_cache: Dict[str, Optional[str]] = {}

    def _resolve_provider_id(token: str) -> Optional[str]:
        """
        Map an import token (dotted module or path-like) back to a NodeId.

        Resolution goes through:
          - canon_module_id → module_to_id
          - canon_file_id   → file_to_id
        We never generate ad-hoc 'core.engine' ids here.
        """
        if not token:
            return None
        if token in _provider_cache:
            return _provider_cache[token]

        nid: Optional[str] = None

        # Try dotted module first
        try:
            m = canon_module_id(token, root)
            nid = module_to_id.get(m)
            if not nid and m.endswith(".__init__"):
                nid = module_to_id.get(m[: -len(".__init__")])
        except Exception:
            pass

        # Try file-id variants
        if not nid:
            try:
                f_py = to_posix(
                    canon_file_id(
                        token,
                        root,
                        prefer_pkg_init=False,
                        strict_suffix=False,
                    )
                )
                f_in = to_posix(
                    canon_file_id(
                        token,
                        root,
                        prefer_pkg_init=True,
                        strict_suffix=False,
                    )
                )
                nid = file_to_id.get(f_py) or file_to_id.get(f_in)
                if not nid and f_in.endswith("/__init__.py"):
                    nid = file_to_id.get(f_in[: -len("/__init__.py")])
            except Exception:
                pass

        _provider_cache[token] = nid
        return nid

    def _looks_like_pkg_init_router_ids(src_id: Optional[str], dst_id: Optional[str]) -> bool:
        """
        Heuristic: detect intra-package __init__.py ↔ submodule edges using file-style ids.

        Examples (True):
          mini_graph/__init__.py -> mini_graph/a.py
          mini_graph/__init__.py -> mini_graph/b/c.py

        Non-examples (False):
          __init__.py -> something.py          # ambiguous top-level
          a.py -> b.py                         # no __init__ involved
          pkg/__init__.py -> otherpkg/a.py     # crosses package boundary
        """
        def _norm(s: Optional[str]) -> str:
            return (s or "").replace("\\", "/")

        s = _norm(src_id)
        d = _norm(dst_id)

        def _is_init(fid: str) -> bool:
            return fid.endswith("/__init__.py") or fid == "__init__.py"

        # One side must be __init__
        if not (_is_init(s) or _is_init(d)):
            return False

        init = s if _is_init(s) else d
        other = d if init is s else s

        # Strip package init suffix; bare "__init__.py" is too ambiguous
        if init.endswith("/__init__.py"):
            pkg = init[: -len("/__init__.py")]
        else:
            return False

        if not pkg:
            return False

        # Other must live inside same package and be a .py file
        return other.endswith(".py") and other.startswith(pkg + "/")

    # --- diagnostics counters ---
    parsed_from_rows = 0
    parsed_import_rows = 0
    added_from = 0
    added_import = 0
    added_fallback = 0
    skipped_self = 0
    no_provider = 0

    # 5) Infer and append edges
    for importer_id, rows in imports_rows_by_id.items():
        importer_payload = out_nodes.get(importer_id) or {}
        importer_mod = importer_payload.get("module") or _norm_node_id_to_module(
            importer_id
        )

        for line in rows:
            s = (line or "").strip()
            if not s:
                continue

            # Case 1: from X import a, b as c
            mod_tok, pairs = _parse_from_row(s)
            if mod_tok and pairs:
                parsed_from_rows += 1
                abs_mod = (
                    _resolve_relative_module(importer_mod, mod_tok)
                    if mod_tok.startswith(".")
                    else mod_tok
                )
                provider_id = _resolve_provider_id(abs_mod or "")
                if not provider_id:
                    no_provider += 1
                    continue
                if provider_id == importer_id:
                    skipped_self += 1
                    continue
                pair = (provider_id, importer_id)
                if pair in existing_pairs:
                    continue
                for name, alias in pairs:
                    e = build_import_edge(
                        imported_id=provider_id,
                        importer_id=importer_id,
                        kind="import",
                        src_contact={"symbol": name},
                        dst_contact={"as": alias or name},
                        idx=0,
                    )

                    if _looks_like_pkg_init_router_ids(provider_id, importer_id):
                        meta = dict(e.get("meta") or {})
                        meta.setdefault("synth_pkg_router", True)
                        e["meta"] = meta
                        e.setdefault("provenance", "synth_pkg_router")

                    k = edge_dedupe_key(e)
                    if k not in seen:
                        seen.add(k)
                        edges.append(e)
                        added_from += 1
                        existing_pairs.add(pair)
                continue

            # Case 2: import A, B as Z
            mods = _parse_import_row(s)
            if mods:
                parsed_import_rows += 1
                for m in mods:
                    abs_mod = (
                        _resolve_relative_module(importer_mod, m)
                        if (isinstance(m, str) and m.startswith("."))
                        else m
                    )
                    provider_id = _resolve_provider_id(abs_mod or "")
                    if not provider_id:
                        no_provider += 1
                        continue
                    if provider_id == importer_id:
                        skipped_self += 1
                        continue
                    pair = (provider_id, importer_id)
                    if pair in existing_pairs:
                        continue
                    e = build_import_edge(
                        imported_id=provider_id,
                        importer_id=importer_id,
                        kind="import_module",
                        idx=0,
                    )

                    if _looks_like_pkg_init_router_ids(provider_id, importer_id):
                        meta = dict(e.get("meta") or {})
                        meta.setdefault("synth_pkg_router", True)
                        e["meta"] = meta
                        e.setdefault("provenance", "synth_pkg_router")

                    k = edge_dedupe_key(e)
                    if k not in seen:
                        seen.add(k)
                        edges.append(e)
                        added_import += 1
                        existing_pairs.add(pair)
                continue

            # Case 3: bare token fallback
            tok = token_to_module(s)
            if tok:
                abs_mod = (
                    _resolve_relative_module(importer_mod, tok)
                    if tok.startswith(".")
                    else tok
                )
                provider_id = _resolve_provider_id(abs_mod or "")
                if not provider_id:
                    no_provider += 1
                elif provider_id == importer_id:
                    skipped_self += 1
                else:
                    pair = (provider_id, importer_id)
                    if pair in existing_pairs:
                        continue
                    e = build_import_edge(
                        imported_id=provider_id,
                        importer_id=importer_id,
                        kind="import_module",
                        idx=0,
                    )

                    if _looks_like_pkg_init_router_ids(provider_id, importer_id):
                        meta = dict(e.get("meta") or {})
                        meta.setdefault("synth_pkg_router", True)
                        e["meta"] = meta
                        e.setdefault("provenance", "synth_pkg_router")

                    k = edge_dedupe_key(e)
                    if k not in seen:
                        seen.add(k)
                        edges.append(e)
                        added_fallback += 1
                        existing_pairs.add(pair)

    g["edges"] = edges

    # --- final diagnostics ---
    try:
        uniq_pairs_out = len(
            {(str(e.get("src")), str(e.get("dst"))) for e in edges if e}
        )
    except Exception:
        uniq_pairs_out = -1
    _log(
        "ANALYZER_BRIDGE:infer_edges_end",
        edges_out=len(edges),
        uniq_pairs=uniq_pairs_out,
        parsed_from_rows=parsed_from_rows,
        parsed_import_rows=parsed_import_rows,
        added_from=added_from,
        added_import=added_import,
        added_fallback=added_fallback,
        skipped_self=skipped_self,
        no_provider=no_provider,
    )

    return g


def enrich_import_provenance(
    graph: Any,
    *,
    repo_root: Optional[Path | str] = None,
) -> Dict[str, Any]:
    """
    Backward-compatible alias for infer_edges_from_import.
    """
    return infer_edges_from_import(graph, repo_root=repo_root)


def normalize_graph_for_contracts(
    graph: Any,
    *,
    repo_root: Optional[Path | str] = None,
    default_reason_kind: str = "import",
    snippet_limit: int = 512,
    max_reasons: Optional[int] = None,
    max_evidence: Optional[int] = None,
    pairwise_by_endpoints: bool = True,
) -> Dict[str, Any]:

    def _ek(g_like: Any) -> int:
        try:
            return len(g_like.get("edges") or [])
        except Exception:
            return -1

    # identify the immediate caller (file:line -> func)
    try:
        import inspect

        frm = inspect.stack()[1]
        caller = f"{frm.filename}:{frm.lineno} in {frm.function}"
    except Exception:
        caller = "<unknown>"

    # --- FAST PATH ---
    if isinstance(graph, dict) and graph.get("_contracts_format") == "normalized":
        _log(
            "ANALYZER_BRIDGE:normalize_fast_path",
            edges=_ek(graph),
            caller=caller,
        )
        return graph

    _log(
        "ANALYZER_BRIDGE:normalize_begin",
        edges_in=_ek(graph),
        caller=caller,
    )

    # Derive repo_root if missing (Windows-safe)
    rr = repo_root
    if rr is None:
        try:
            nodes = dict(
                getattr(graph, "nodes", {})
                or (graph.get("nodes") if isinstance(graph, dict) else {})
                or {}
            )
            abs_files: list[str] = []
            for n in nodes.values():
                fp = (
                    getattr(n, "file", None)
                    or getattr(n, "path", None)
                    or (n.get("file") if isinstance(n, dict) else None)
                    or (n.get("path") if isinstance(n, dict) else None)
                )
                if isinstance(fp, str) and os.path.isabs(fp):
                    abs_files.append(fp)
            if abs_files:
                base = os.path.commonpath(abs_files)
                if base and os.path.isdir(base):
                    rr = base
        except Exception:
            rr = None

    # 1) infer import edges (and normalize node payloads)
    g = infer_edges_from_import(graph, repo_root=rr)
    _log(
        "ANALYZER_BRIDGE:normalize_after_infer",
        edges=_ek(g),
        caller=caller,
    )

    # 2) attach meta.root if absent
    try:
        if isinstance(g, dict):
            meta = g.setdefault("meta", {})
            if "root" not in meta and "root" not in g and rr:
                nr = normalize_root(rr)
                if nr:
                    meta["root"] = str(nr)
    except Exception:
        pass

    # 3) provenance tagging
    try:
        g = tag_import_provenance(g)
    except Exception as e:
        _log(
            "ANALYZER_BRIDGE:provenance_error",
            err_type=type(e).__name__,
            error=str(e),
        )

    # *** FIX: consistent stamp with underscore so fast-path works ***
    try:
        g["_contracts_format"] = "normalized"
    except Exception:
        pass

    _log(
        "ANALYZER_BRIDGE:normalize_end",
        edges_out=_ek(g),
        stamped=g.get("_contracts_format"),
        caller=caller,
    )
    return g
