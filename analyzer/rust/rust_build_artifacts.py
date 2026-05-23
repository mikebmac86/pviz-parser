from __future__ import annotations

"""
Rust artifact builders (FolderIndex -> NodeFacts + Edges).

UPDATED to support the expanded RustParsedFile model (traits/impls/derives/attributes/type_aliases,
unsafe/async flags, problems, loc_code, etc.) WITHOUT requiring a compiler.

Key upgrades in this revision:
  - Cache-aware symbol extraction that can consume RustParsedFile records directly
    (no re-parse; best-effort conversion to NodeFactsSymbols).
  - NodeFacts enrichment (optional, schema-safe additive keys) when cache records
    have richer fields (traits/impls/derives/attributes/type_aliases/mod_decls).
  - Edge enrichment upgraded to use the expanded model safely (derive edges,
    trait supertrait edges, impl->trait edges, and optional module declaration edges).
  - Keeps nodefacts@v1.6 + edges@v1 compatibility (extra per-node keys are additive).
  - Preserves raw Rust use/import specs from FileEntry.imports_all as imports_all_raw,
    separate from resolved graph imports.

Schema constraints:
  - NodeFacts: nodefacts@v1.6 compatible (nodes dict, meta)
    - scc_id / scc_size are computed from the Rust internal dependency graph
  - Edges: edges@v1 compatible (edges list of {src,dst,kind,confidence})
"""

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from concurrent.futures import ProcessPoolExecutor, as_completed
import time
import os

from analyzer_store.types import FileEntry, FolderIndex
from adapters.canonical import to_posix

from analyzer.rust.rust_canonical import (
    normalize_rust_use_path,
    resolve_rust_use,
)

from analyzer.rust.rust_nodefacts_symbols import NodeFactsSymbols, parse_symbols_for_nodefacts

# Diagnostics logging
try:
    from diagnostics.logging import log_event  # type: ignore
except Exception:  # pragma: no cover
    def log_event(*_a, **_k):
        return


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _stable_unique_strs(vals: Iterable[str]) -> Tuple[str, ...]:
    s: Set[str] = set()
    for v in vals or ():
        if not v:
            continue
        vv = str(v).strip()
        if vv:
            s.add(vv)
    return tuple(sorted(s))


def _norm_parse_status(s: str) -> str:
    """
    Normalize parse statuses into: ok | warn | error
    Keeps this tolerant because upstream sources may emit many variants.
    """
    ss = str(s or "").strip().lower()
    if not ss:
        return "ok"
    if "error" in ss or "fail" in ss or "exception" in ss or ss in {"err"}:
        return "error"
    if "warn" in ss or ss in {"partial", "degraded"}:
        return "warn"
    if ss in {"ok", "success"}:
        return "ok"
    # Unknown -> warn (safer than pretending ok)
    return "warn"


def _upgrade_parse_status(cur: str, new: str) -> str:
    """
    Promote parse status across multiple sources:

      ok > warn > error
    """
    cur_n = _norm_parse_status(cur)
    new_n = _norm_parse_status(new)
    rank = {"ok": 0, "warn": 1, "error": 2}
    return new_n if rank.get(new_n, 0) > rank.get(cur_n, 0) else cur_n


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


def _rust_name_from_id(node_id: str) -> str:
    s = str(node_id or "").strip()
    if not s:
        return ""
    if "/" in s:
        return s.rsplit("/", 1)[-1]
    if "::" in s:
        return s.rsplit("::", 1)[-1]
    return s


def _stub_node(node_id: str) -> Dict[str, Any]:
    nid = str(node_id or "").strip()
    return {
        "id": nid,
        "name": _rust_name_from_id(nid) or nid,
        "language": "rust",
        "lang": "rust",
        "imports": (),
        "imports_all_raw": (),
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
        "crosstalk_candidates_rust_v1": (),

        # rust enrichment (optional)
        "module_path": None,
        "declared_types": (),
        "declared_types_fq": (),
        "public_exports": (),
        "annotations": (),
    }


def _rust_nf_cfg_payload(cfg: Any) -> Dict[str, Any]:
    """
    Small, picklable cfg payload for worker processes (and cache-path conversions).
    """
    return {
        "max_file_bytes": getattr(cfg, "max_file_bytes", None),
        "max_bytes_per_file": getattr(cfg, "max_bytes_per_file", None),
        "include_tests": getattr(cfg, "include_tests", None),
        "rustparser_cli_path": getattr(cfg, "rustparser_cli_path", None),
    }


def _chunk(seq: List[str], n: int) -> List[List[str]]:
    chunks: List[List[str]] = []
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


def _chunk_by_bytes(
    rel_files: List[str],
    *,
    repo_root: Path,
    max_workers: int,
    target_batch_bytes: int = 256_000,
) -> List[List[str]]:
    batches: List[List[str]] = [[]]
    cur_bytes = 0

    for rf in rel_files:
        try:
            size = (repo_root / rf).stat().st_size
        except Exception:
            size = 8_000

        if cur_bytes + size > target_batch_bytes and batches[-1]:
            batches.append([])
            cur_bytes = 0

        batches[-1].append(rf)
        cur_bytes += size

    if len(batches) < max_workers:
        return _chunk(rel_files, max_workers)

    return batches


def _is_like_parsed_file(x: object) -> bool:
    """
    Structural check for RustParsedFile-like objects.
    We avoid isinstance checks because cache objects may originate from a different import context.
    """
    try:
        if x is None:
            return False
        # strong hints
        if hasattr(x, "parse_status") and hasattr(x, "ok"):
            return True
        # weaker hints
        if hasattr(x, "use_statements") and hasattr(x, "functions"):
            return True
    except Exception:
        pass
    return False


class _SymsShim:
    """
    Minimal attribute-bag compatible with NodeFacts builder consumption.
    We use this when NodeFactsSymbols construction differs across contexts.
    """
    def __init__(
        self,
        *,
        ok: bool,
        parse_status: str,
        module_path: Optional[str],
        loc_code: Optional[int],
        exports: Tuple[str, ...],
        classes: Tuple[str, ...],
        functions: Tuple[str, ...],
        globals: Tuple[str, ...],
        declared_types: Tuple[str, ...],
        declared_types_fq: Tuple[str, ...],
        public_exports: Tuple[str, ...],
        annotations: Tuple[str, ...],
    ) -> None:
        self.ok = ok
        self.parse_status = parse_status
        self.module_path = module_path
        self.loc_code = loc_code
        self.exports = exports
        self.classes = classes
        self.functions = functions
        self.globals = globals
        self.declared_types = declared_types
        self.declared_types_fq = declared_types_fq
        self.public_exports = public_exports
        self.annotations = annotations


def _parsedfile_to_nodefacts_symbols(rec: Any, cfg_payload: Dict[str, Any]) -> Optional[NodeFactsSymbols]:
    """
    Best-effort conversion: RustParsedFile -> NodeFactsSymbols

    Preferred: call parse_symbols_for_nodefacts(rec, cfg_payload) if it supports it.
    Fallback: synthesize a shim with the fields NodeFacts builder actually consumes.
    """
    # Preferred: unified call-shape (if supported by your implementation)
    try:
        syms = parse_symbols_for_nodefacts(rec, cfg_payload)
        return syms
    except Exception:
        pass

    # Fallback synthesis (populate the fields the builder expects)
    try:
        ok = bool(getattr(rec, "ok", True))
        parse_status = _norm_parse_status(str(getattr(rec, "parse_status", "ok") or "ok"))
        module_path = str(getattr(rec, "module_path", "") or "").strip() or None
        loc_code = getattr(rec, "loc_code", None)

        # classes: structs + enums + traits
        struct_objs = getattr(rec, "structs", None) or ()
        enum_objs = getattr(rec, "enums", None) or ()
        trait_objs = getattr(rec, "traits", None) or ()
        fn_objs = getattr(rec, "functions", None) or ()

        struct_names = tuple(sorted({
            str(getattr(s, "name", "") or "").strip()
            for s in struct_objs
            if str(getattr(s, "name", "") or "").strip()
        }))
        enum_names = tuple(sorted({
            str(getattr(e, "name", "") or "").strip()
            for e in enum_objs
            if str(getattr(e, "name", "") or "").strip()
        }))
        trait_names = tuple(sorted({
            str(getattr(t, "name", "") or "").strip()
            for t in trait_objs
            if str(getattr(t, "name", "") or "").strip()
        }))
        fn_names = tuple(sorted({
            str(getattr(f, "name", "") or "").strip()
            for f in fn_objs
            if str(getattr(f, "name", "") or "").strip()
        }))

        classes = tuple(sorted(set(struct_names) | set(enum_names) | set(trait_names)))

        # exports: pub items
        exports_set: Set[str] = set()
        for s in struct_objs:
            if bool(getattr(s, "is_pub", False)):
                n = str(getattr(s, "name", "") or "").strip()
                if n:
                    exports_set.add(n)
        for e in enum_objs:
            if bool(getattr(e, "is_pub", False)):
                n = str(getattr(e, "name", "") or "").strip()
                if n:
                    exports_set.add(n)
        for t in trait_objs:
            if bool(getattr(t, "is_pub", False)):
                n = str(getattr(t, "name", "") or "").strip()
                if n:
                    exports_set.add(n)
        for f in fn_objs:
            if bool(getattr(f, "is_pub", False)):
                n = str(getattr(f, "name", "") or "").strip()
                if n:
                    exports_set.add(n)
        exports = tuple(sorted(exports_set))

        # globals: best-effort legacy struct fields list (names only)
        globals_set: Set[str] = set()
        for s in struct_objs:
            try:
                fields = getattr(s, "fields", None) or ()
                for v in fields:
                    vv = str(v or "").strip()
                    if vv:
                        globals_set.add(vv)
            except Exception:
                continue
        globals_ = tuple(sorted(globals_set))

        # Declared types: align with classes (safe, simple)
        declared_types = classes
        declared_types_fq = tuple()  # only if you have per-symbol FQ info elsewhere
        public_exports = exports
        annotations = tuple()  # keep empty unless you have a safe source

        shim = _SymsShim(
            ok=ok,
            parse_status=parse_status,
            module_path=module_path,
            loc_code=loc_code,
            exports=exports,
            classes=classes,
            functions=fn_names,
            globals=globals_,
            declared_types=declared_types,
            declared_types_fq=declared_types_fq,
            public_exports=public_exports,
            annotations=annotations,
        )
        return shim  # type: ignore[return-value]
    except Exception:
        return None


def _rust_nf_build_symbols_batch(
    repo_root_str: str,
    rel_files: List[str],
    cfg_payload: Dict[str, Any],
) -> Tuple[Dict[str, Optional[NodeFactsSymbols]], Dict[str, str], Dict[str, str]]:
    """
    Batch worker: parse symbols for rel_files and return:
      - syms_by_relfile
      - status_by_relfile
      - file_id_to_module (BEST-EFFORT; keyed by rel_file POSIX)
    """
    repo_root = Path(repo_root_str)

    syms_by_rel: Dict[str, Optional[NodeFactsSymbols]] = {}
    status_by_rel: Dict[str, str] = {}
    module_by_relposix: Dict[str, str] = {}

    for rel in rel_files:
        rel_file = str(rel or "").strip()
        if not rel_file:
            continue

        try:
            abs_path = (repo_root / rel_file).resolve(strict=False)
            syms = parse_symbols_for_nodefacts(abs_path, cfg_payload)
            st = getattr(syms, "parse_status", "ok") if syms is not None else "error"
        except Exception:
            syms, st = None, "error"

        syms_by_rel[rel_file] = syms
        status_by_rel[rel_file] = _norm_parse_status(str(st or "ok"))

        if syms is not None:
            try:
                module = str(getattr(syms, "module_path", "") or "").strip()
                relp = to_posix(rel_file)
                if module and relp:
                    module_by_relposix[relp] = module
            except Exception:
                pass

    return syms_by_rel, status_by_rel, module_by_relposix


# ---------------------------------------------------------------------------
# NodeFacts builder (from FolderIndex)
# ---------------------------------------------------------------------------

def build_nodefacts_from_folder_index(
    idx: FolderIndex,
    *,
    repo_root: Path,
    cfg: Any,
    module_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build NodeFacts from a Rust FolderIndex.

    Supports parse cache:
      - cfg.rust_parse_cache: Dict[...] (from folder_index phase)
        Values may be RustParsedFile records.

    IMPORTANT:
      - The canonical cache key is FileEntry.id (repo-relative POSIX file id).
      - We still allow fallback lookup by rel_file / abs_path for transition, but we track it.

    Import fields:
      - imports: resolved internal dependency targets used for graph edges/SCCs.
      - imports_all_raw: normalized Rust use specs from FileEntry.imports_all.
        This preserves external/unresolved/raw use surface without polluting the graph.
    """
    t0 = time.perf_counter()

    repo_root = Path(repo_root).resolve()
    files = getattr(idx, "files", {}) or {}

    node_id_space = str(getattr(cfg, "rust_node_id_space", "file") or "file").strip().lower()
    symbols_mode = str(getattr(cfg, "rust_nodefacts_symbols", "dispatcher") or "dispatcher").strip().lower()

    if node_id_space == "module" and symbols_mode == "none":
        log_event("RUST:nodefacts_module_mode_disabled", reason="symbols_mode=none")
        node_id_space = "file"

    node_loc_total: Dict[str, int] = {}
    node_sloc_total: Dict[str, int] = {}
    node_comment_lines_total: Dict[str, int] = {}
    node_blank_lines_total: Dict[str, int] = {}
    node_file_count: Dict[str, int] = {}

    by_node: Dict[str, Dict[str, Any]] = {}
    node_rep_file: Dict[str, str] = {}

    parsed_ok_files = 0
    parsed_warn_files = 0
    parsed_err_files = 0

    fe_list: List[FileEntry] = [fe for fe in files.values() if isinstance(fe, FileEntry)]
    fe_list.sort(
        key=lambda fe: (
            to_posix(str(getattr(fe, "id", "") or "").strip()),
            to_posix(str(getattr(fe, "file", "") or "").strip()),
        )
    )

    syms_by_relfile: Dict[str, Optional[NodeFactsSymbols]] = {}
    syms_status_by_relfile: Dict[str, str] = {}

    # file-id space module map (keyed by FileEntry.id POSIX)
    file_id_to_module: Dict[str, str] = {}

    parse_cache = getattr(cfg, "rust_parse_cache", None)
    cfg_payload = _rust_nf_cfg_payload(cfg)

    # Optional per-node enrichment from cache (no schema change; additive)
    enrich_from_cache = bool(getattr(cfg, "rust_nodefacts_enrichment", True))
    enrich_keys = (
        "rust_traits",
        "rust_impl_traits",
        "rust_derives",
        "rust_attributes",
        "rust_type_aliases",
        "rust_mod_decls",
    )
    sym_keys = (
        "public_exports",
        "declared_types",
        "declared_types_fq",
        "annotations",
        "exports",
        "classes",
        "functions",
        "globals",
    )

    # -------------------------
    # Symbol extraction (cache-aware)
    # -------------------------
    if symbols_mode != "none":
        rel_files: List[str] = []
        for fe in fe_list:
            rel_file = str(getattr(fe, "file", "") or "").strip()
            if rel_file:
                rel_files.append(rel_file)

        # Canonical cache lookups
        cache_hits = 0
        cache_misses = 0
        cache_fallback_hits = 0

        def _cache_get(*, fe: FileEntry) -> Optional[object]:
            """
            Canonical cache key is FileEntry.id (POSIX repo-relative).
            We still allow fallback keys for transition but track them.
            """
            nonlocal cache_fallback_hits
            if not isinstance(parse_cache, dict) or not parse_cache:
                return None

            fe_id = to_posix(str(getattr(fe, "id", "") or "").strip())
            rel_file = str(getattr(fe, "file", "") or "").strip()
            relp = to_posix(rel_file)

            # 1) canonical
            if fe_id and fe_id in parse_cache:
                return parse_cache[fe_id]

            # 2) transitional fallbacks
            for k in (rel_file, relp):
                if k and k in parse_cache:
                    cache_fallback_hits += 1
                    return parse_cache[k]

            try:
                if rel_file:
                    abs_key = str((repo_root / rel_file).resolve(strict=False))
                    if abs_key in parse_cache:
                        cache_fallback_hits += 1
                        return parse_cache[abs_key]
            except Exception:
                pass

            return None

        if rel_files and isinstance(parse_cache, dict) and parse_cache:
            log_event(
                "RUST_NODEFACTS:using_parse_cache",
                root=str(repo_root),
                cache_size=len(parse_cache),
                files_needed=len(rel_files),
            )

            t_transform = time.perf_counter()

            for fe in fe_list:
                rel_file = str(getattr(fe, "file", "") or "").strip()
                if not rel_file:
                    continue

                cached = _cache_get(fe=fe)

                syms: Optional[NodeFactsSymbols] = None
                try:
                    if cached is not None and _is_like_parsed_file(cached):
                        syms = _parsedfile_to_nodefacts_symbols(cached, cfg_payload)
                        cache_hits += 1
                    else:
                        abs_path = (repo_root / rel_file).resolve(strict=False)
                        syms = parse_symbols_for_nodefacts(abs_path, cfg_payload)
                        cache_misses += 1
                except Exception:
                    syms = None

                syms_by_relfile[rel_file] = syms

                st = getattr(syms, "parse_status", None) if syms is not None else None
                if not st and cached is not None and _is_like_parsed_file(cached):
                    st = getattr(cached, "parse_status", None)
                syms_status_by_relfile[rel_file] = _norm_parse_status(str(st or "error"))

                # module map in file-id space (canonical)
                try:
                    module = ""
                    if syms is not None:
                        module = str(getattr(syms, "module_path", "") or "").strip()
                    elif cached is not None and _is_like_parsed_file(cached):
                        module = str(getattr(cached, "module_path", "") or "").strip()

                    fidp = to_posix(str(getattr(fe, "id", "") or "").strip())
                    if module and fidp:
                        file_id_to_module[fidp] = module
                except Exception:
                    pass

            log_event(
                "RUST_NODEFACTS:cache_transform_done",
                root=str(repo_root),
                cache_hits=cache_hits,
                cache_misses=cache_misses,
                cache_fallback_hits=cache_fallback_hits,
                ms=int((time.perf_counter() - t_transform) * 1000),
                module_map=len(file_id_to_module),
            )

        elif rel_files:
            # Non-cache: parallel parsing (with small repo guardrail)
            cpu_count = os.cpu_count() or 1
            cfg_workers = int(getattr(cfg, "rust_max_workers", 0) or 0)
            base_workers = int(getattr(cfg, "max_workers", cpu_count) or cpu_count)
            want = cfg_workers if cfg_workers > 0 else base_workers
            max_workers = min(max(1, want), len(rel_files))

            # avoid process-pool overhead on small batches
            if max_workers <= 1 or len(rel_files) < 10:
                batch_syms, batch_status, batch_module = _rust_nf_build_symbols_batch(
                    str(repo_root), rel_files, cfg_payload
                )
                syms_by_relfile.update(batch_syms)
                syms_status_by_relfile.update(batch_status)
                for fe in fe_list:
                    fidp = to_posix(str(getattr(fe, "id", "") or "").strip())
                    relp = to_posix(str(getattr(fe, "file", "") or "").strip())
                    module = batch_module.get(relp, "")
                    if fidp and module:
                        file_id_to_module[fidp] = module
            else:
                # keep workers sane
                max_workers = min(max_workers, int(getattr(cfg, "rust_max_workers_cap", 8) or 8))
                batches = _chunk_by_bytes(rel_files, repo_root=repo_root, max_workers=max_workers)

                relposix_to_module: Dict[str, str] = {}
                with ProcessPoolExecutor(max_workers=max_workers) as ex:
                    futures = {
                        ex.submit(_rust_nf_build_symbols_batch, str(repo_root), batch, cfg_payload): idxb
                        for idxb, batch in enumerate(batches)
                    }
                    for fut in as_completed(futures):
                        try:
                            batch_syms, batch_status, batch_module = fut.result()
                        except Exception:
                            batch_syms, batch_status, batch_module = {}, {}, {}
                        syms_by_relfile.update(batch_syms)
                        syms_status_by_relfile.update(batch_status)
                        relposix_to_module.update(batch_module)

                for fe in fe_list:
                    fidp = to_posix(str(getattr(fe, "id", "") or "").strip())
                    relp = to_posix(str(getattr(fe, "file", "") or "").strip())
                    module = relposix_to_module.get(relp, "")
                    if fidp and module:
                        file_id_to_module[fidp] = module

    # -------------------------
    # module mode safety gate
    # -------------------------
    if node_id_space == "module":
        want = sum(1 for fe in fe_list if to_posix(str(getattr(fe, "id", "") or "").strip()))
        have = len(file_id_to_module)
        if have < max(1, int(want * 0.95)):
            log_event(
                "RUST:nodefacts_module_mode_disabled",
                reason="insufficient_module_map_file_id_space",
                have=have,
                want=want,
            )
            node_id_space = "file"

    # publish map for downstream
    try:
        setattr(cfg, "rust_file_id_to_module", dict(file_id_to_module))
    except Exception:
        pass

    def _node_id_for_file(*, fe: FileEntry) -> str:
        if node_id_space == "module":
            fid = to_posix(str(getattr(fe, "id", "") or "").strip())
            module = str(file_id_to_module.get(fid, "") or "").strip()
            return module or "crate"
        return to_posix(str(getattr(fe, "id", "") or "").strip())

    # helper: get cached record for enrichment (canonical first)
    def _cache_get_for_fe(fe: FileEntry) -> Optional[object]:
        if not isinstance(parse_cache, dict) or not parse_cache:
            return None
        fe_id = to_posix(str(getattr(fe, "id", "") or "").strip())
        if fe_id and fe_id in parse_cache:
            return parse_cache[fe_id]
        rel_file = str(getattr(fe, "file", "") or "").strip()
        relp = to_posix(rel_file)
        for k in (rel_file, relp):
            if k and k in parse_cache:
                return parse_cache[k]
        try:
            if rel_file:
                abs_key = str((repo_root / rel_file).resolve(strict=False))
                if abs_key in parse_cache:
                    return parse_cache[abs_key]
        except Exception:
            pass
        return None

    # -------------------------
    # Build nodes (aggregation)
    # -------------------------
    for fe in fe_list:
        rel_file = str(getattr(fe, "file", "") or "").strip()
        if not rel_file:
            continue

        syms: Optional[NodeFactsSymbols] = syms_by_relfile.get(rel_file) if symbols_mode != "none" else None
        node_id = _node_id_for_file(fe=fe)
        if not node_id:
            continue

        fe_loc = max(int(getattr(fe, "loc", 0) or 0), 0)
        fe_sloc = max(int(getattr(fe, "sloc", 0) or 0), 0)
        fe_comment_lines = max(int(getattr(fe, "comment_lines", 0) or 0), 0)
        fe_blank_lines = max(int(getattr(fe, "blank_lines", 0) or 0), 0)

        node_loc_total[node_id] = node_loc_total.get(node_id, 0) + fe_loc
        node_sloc_total[node_id] = node_sloc_total.get(node_id, 0) + fe_sloc
        node_comment_lines_total[node_id] = node_comment_lines_total.get(node_id, 0) + fe_comment_lines
        node_blank_lines_total[node_id] = node_blank_lines_total.get(node_id, 0) + fe_blank_lines
        node_file_count[node_id] = node_file_count.get(node_id, 0) + 1

        sym_status = "ok"
        if symbols_mode != "none":
            sym_status = syms_status_by_relfile.get(
                rel_file,
                getattr(syms, "parse_status", "ok") if syms is not None else "error",
            )

        status = _upgrade_parse_status(
            str(getattr(fe, "parse_status", "ok") or "ok"),
            str(sym_status or "ok"),
        )

        if status == "ok":
            parsed_ok_files += 1
        elif status == "warn":
            parsed_warn_files += 1
        else:
            parsed_err_files += 1

        if node_id not in by_node:
            node_rep_file[node_id] = rel_file
            by_node[node_id] = {
                "id": node_id,
                "name": _rust_name_from_id(node_id) or node_id,
                "language": "rust",
                "lang": "rust",

                # graph-facing imports
                "imports": (),
                "importers_count": 0,
                "dependencies_count": 0,

                # raw parser/folder-index use surface
                "imports_all_raw": (),

                "exports": (),
                "classes": (),
                "functions": (),
                "globals": (),

                # rust enrichment fields (best-effort)
                "module_path": (str(getattr(syms, "module_path", "") or "").strip() or None) if syms is not None else None,
                "declared_types": (),
                "declared_types_fq": (),
                "public_exports": (),
                "annotations": (),

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

                "crosstalk_candidates_rust_v1": (),
            }

            # scratch accumulators
            by_node[node_id]["__acc_imports_all_raw"] = set()
            for k in sym_keys:
                by_node[node_id][f"__acc_{k}"] = set()  # type: ignore[assignment]
            for k in enrich_keys:
                by_node[node_id][f"__acc_{k}"] = set()  # type: ignore[assignment]
        else:
            by_node[node_id]["parse_status"] = _upgrade_parse_status(by_node[node_id].get("parse_status", "ok"), status)

        node = by_node[node_id]

        # Preserve raw Rust use/import specs from FolderIndex.
        #
        # This is intentionally separate from node["imports"], which is reserved for
        # resolved internal dependency targets used by graph edges/SCCs.
        raw_acc = node.get("__acc_imports_all_raw")
        if isinstance(raw_acc, set):
            for raw in (getattr(fe, "imports_all", None) or ()):
                spec = str(raw or "").strip()
                if spec:
                    raw_acc.add(spec)

        # prefer non-empty module path
        if syms is not None:
            try:
                mp = str(getattr(syms, "module_path", "") or "").strip()
                if mp:
                    node["module_path"] = mp
            except Exception:
                pass

        # merge NodeFactsSymbols -> sets
        if syms is not None:
            for k in sym_keys:
                try:
                    vals = getattr(syms, k, ()) or ()
                except Exception:
                    vals = ()
                acc = node.get(f"__acc_{k}")
                if isinstance(acc, set):
                    for v in vals:
                        vv = str(v or "").strip()
                        if vv:
                            acc.add(vv)

        # OPTIONAL: enrich from cached RustParsedFile (traits/impls/derives/attributes/etc.)
        if enrich_from_cache:
            rec = _cache_get_for_fe(fe)
            if rec is not None and _is_like_parsed_file(rec):
                try:
                    traits = getattr(rec, "traits", None) or ()
                    acc = node.get("__acc_rust_traits")
                    if isinstance(acc, set):
                        for t in traits:
                            name = str(getattr(t, "name", "") or "").strip()
                            if name:
                                acc.add(name)
                except Exception:
                    pass

                try:
                    impls = getattr(rec, "impls", None) or ()
                    acc = node.get("__acc_rust_impl_traits")
                    if isinstance(acc, set):
                        for im in impls:
                            tn = str(getattr(im, "trait_name", "") or "").strip()
                            if tn:
                                acc.add(tn)
                except Exception:
                    pass

                try:
                    acc = node.get("__acc_rust_derives")
                    if isinstance(acc, set):
                        for st in (getattr(rec, "structs", None) or ()):
                            for d in (getattr(st, "derives", None) or ()):
                                dd = str(d or "").strip()
                                if dd:
                                    acc.add(dd)
                        for en in (getattr(rec, "enums", None) or ()):
                            for d in (getattr(en, "derives", None) or ()):
                                dd = str(d or "").strip()
                                if dd:
                                    acc.add(dd)
                except Exception:
                    pass

                try:
                    attrs = getattr(rec, "attributes", None) or ()
                    acc = node.get("__acc_rust_attributes")
                    if isinstance(acc, set):
                        for a in attrs:
                            aa = str(a or "").strip()
                            if aa:
                                acc.add(aa)
                except Exception:
                    pass

                try:
                    acc = node.get("__acc_rust_type_aliases")
                    if isinstance(acc, set):
                        for ta in (getattr(rec, "type_aliases", None) or ()):
                            name = str(getattr(ta, "name", "") or "").strip()
                            if name:
                                acc.add(name)
                except Exception:
                    pass

                try:
                    acc = node.get("__acc_rust_mod_decls")
                    if isinstance(acc, set):
                        for md in (getattr(rec, "mod_declarations", None) or ()):
                            name = str(getattr(md, "name", "") or "").strip()
                            if name:
                                acc.add(name)
                except Exception:
                    pass

    # deps/importers from imports_internal
    deps_by_node: Dict[str, Set[str]] = {}
    importers_count: Dict[str, int] = {}

    for fe in fe_list:
        src_file = to_posix(str(getattr(fe, "id", "") or "").strip())
        if not src_file:
            continue
        src = file_id_to_module.get(src_file, "") if node_id_space == "module" else src_file
        if not src:
            continue

        deps = deps_by_node.setdefault(src, set())
        for dst0 in (getattr(fe, "imports_internal", None) or ()):
            dst_file = to_posix(str(dst0 or "").strip())
            if not dst_file:
                continue
            dst = file_id_to_module.get(dst_file, "") if node_id_space == "module" else dst_file
            if not dst or dst == src:
                continue
            deps.add(dst)
            importers_count[dst] = importers_count.get(dst, 0) + 1

    # ensure stubs for unseen referenced nodes
    for src, deps in deps_by_node.items():
        if src not in by_node:
            by_node[src] = _stub_node(src)
        for dst in deps:
            if dst not in by_node:
                by_node[dst] = _stub_node(dst)

    # SCC computation from finalized Rust dependency graph
    scc_id_by_node, scc_size_by_id = _compute_scc_from_deps(
        by_node.keys(),
        deps_by_node,
    )

    # finalize nodes
    for node_id, node in by_node.items():
        deps = deps_by_node.get(node_id, set())

        # Graph-facing imports: resolved internal dependency targets.
        node["imports"] = tuple(sorted(deps))

        # Raw Rust use/import specs: preserve full FolderIndex imports_all surface.
        raw_acc = node.pop("__acc_imports_all_raw", None)
        node["imports_all_raw"] = tuple(sorted(raw_acc)) if isinstance(raw_acc, set) else tuple(node.get("imports_all_raw", ()) or ())

        node["dependencies_count"] = len(deps)
        node["importers_count"] = importers_count.get(node_id, 0)
        sid = scc_id_by_node.get(node_id, node_id)
        node["scc_id"] = sid
        node["scc_size"] = scc_size_by_id.get(sid, 1)
        loc_total = int(node_loc_total.get(node_id, 0))
        comment_total = int(node_comment_lines_total.get(node_id, 0))

        node["loc"] = loc_total
        node["sloc"] = int(node_sloc_total.get(node_id, 0))
        node["comment_lines"] = comment_total
        node["blank_lines"] = int(node_blank_lines_total.get(node_id, 0))
        node["comment_pct"] = (comment_total / loc_total) if loc_total else None
        node["file_count"] = int(node_file_count.get(node_id, 0))

        rep = node_rep_file.get(node_id)
        if rep and node.get("file") != rep:
            node["file"] = rep

        for k in sym_keys:
            acc = node.pop(f"__acc_{k}", None)
            node[k] = tuple(sorted(acc)) if isinstance(acc, set) else tuple(node.get(k, ()) or ())

        # additive enrichment keys
        for k in enrich_keys:
            acc = node.pop(f"__acc_{k}", None)
            if isinstance(acc, set) and acc:
                node[k] = tuple(sorted(acc))

    nodes = {k: by_node[k] for k in sorted(by_node.keys())}
    loc_total_all = int(sum(node_loc_total.values()))
    sloc_total_all = int(sum(node_sloc_total.values()))
    comment_lines_total_all = int(sum(node_comment_lines_total.values()))
    blank_lines_total_all = int(sum(node_blank_lines_total.values()))
    comment_pct_total = (comment_lines_total_all / loc_total_all) if loc_total_all else None

    nodes_with_raw_imports = sum(
        1 for node in nodes.values()
        if node.get("imports_all_raw")
    )
    raw_import_specs_total = sum(
        len(node.get("imports_all_raw") or ())
        for node in nodes.values()
    )

    out = {
        "schema_version": "nodefacts@v1.6",
        "language": "rust",
        "nodes": nodes,
        "meta": {
            "module_name": (module_name or ""),
            "nodes": len(nodes),
            "files_indexed": len(files),
            "parse_ok_files": parsed_ok_files,
            "parse_warn_files": parsed_warn_files,
            "parse_err_files": parsed_err_files,
            "loc_total": loc_total_all,
            "sloc_total": sloc_total_all,
            "comment_lines_total": comment_lines_total_all,
            "blank_lines_total": blank_lines_total_all,
            "comment_pct": comment_pct_total,
            "rust_node_id_space": node_id_space,
            "rust_nodefacts_symbols": symbols_mode,
            "used_parse_cache": bool(isinstance(parse_cache, dict) and parse_cache),
            "nodefacts_enrichment": bool(enrich_from_cache),
            "rust_file_id_to_module": dict(file_id_to_module),
            "nodes_with_imports_all_raw": int(nodes_with_raw_imports),
            "raw_import_specs_total": int(raw_import_specs_total),
        },
    }

    log_event(
        "RUST:nodefacts_built",
        nodes=len(nodes),
        nodes_with_imports_all_raw=int(nodes_with_raw_imports),
        raw_import_specs_total=int(raw_import_specs_total),
        ms=int((time.perf_counter() - t0) * 1000),
    )
    return out


# ---------------------------------------------------------------------------
# Edge builder (from FolderIndex)
# ---------------------------------------------------------------------------

def build_edges_from_folder_index(
    idx: FolderIndex,
    *,
    internal_only: bool = True,
    include_external: bool = False,
    drop_self_edges: bool = True,
    cfg: Any = None,
) -> Dict[str, Any]:
    """
    Build an edges@v1 object from a Rust FolderIndex.

    Internal edges:
      - from FileEntry.imports_internal

    Optional enrichment edges (no schema changes):
      - rust:impl:trait        (file/module -> trait path)
      - rust:trait:supertrait  (file/module -> supertrait bound)
      - rust:derive            (file/module -> derive macro name)
      - rust:mod:declares      (file/module -> "mod <name>" declaration) [optional]
      - rust:attribute         (file/module -> attribute token string)   [optional, can be noisy]
    """
    node_id_space = str(getattr(cfg, "rust_node_id_space", "file") or "file").strip().lower() if cfg else "file"

    # If imports_internal may include more than "use" edges, default to a neutral kind.
    internal_kind = str(getattr(cfg, "rust_internal_edge_kind", "rust:dep:internal") or "rust:dep:internal") if cfg else "rust:dep:internal"

    edges_out: List[Dict[str, object]] = []
    seen: Set[Tuple[str, str, str]] = set()

    files = getattr(idx, "files", {}) or {}
    fe_list: List[FileEntry] = [fe for fe in files.values() if isinstance(fe, FileEntry)]

    # Map in file-id space (POSIX)
    file_id_to_module: Dict[str, str] = {}
    if node_id_space == "module" and cfg is not None:
        m = getattr(cfg, "rust_file_id_to_module", None)
        if not isinstance(m, dict) or not m:
            m = getattr(cfg, "rust_file_id_to_module_map", None)
        if not isinstance(m, dict) or not m:
            m = getattr(cfg, "meta_rust_file_id_to_module", None)
        if isinstance(m, dict):
            file_id_to_module = {to_posix(str(k)): str(v) for (k, v) in m.items() if k and v}
        if not file_id_to_module:
            node_id_space = "file"

    def _map_node_id(file_id: str) -> str:
        fid = to_posix(str(file_id or "").strip())
        if not fid:
            return ""
        if node_id_space != "module":
            return fid
        return str(file_id_to_module.get(fid, "") or "").strip()

    def _add_edge(src: str, dst: str, kind: str, confidence: float) -> None:
        if not src or not dst or not kind:
            return
        if drop_self_edges and src == dst:
            return
        key = (src, dst, kind)
        if key in seen:
            return
        seen.add(key)
        edges_out.append({"src": src, "dst": dst, "kind": kind, "confidence": float(confidence)})

    # -------------------------
    # Internal edges
    # -------------------------
    if internal_only:
        for fe in fe_list:
            src_file = to_posix(str(getattr(fe, "id", "") or "").strip())
            src = _map_node_id(src_file)
            if not src:
                continue

            for dst0 in (getattr(fe, "imports_internal", None) or ()):
                dst_file = to_posix(str(dst0 or "").strip())
                dst = _map_node_id(dst_file)
                if not dst:
                    continue
                if drop_self_edges and dst == src:
                    continue
                _add_edge(src, dst, internal_kind, 0.8)

    # -------------------------
    # External edges (optional)
    # -------------------------
    if include_external:
        module_to_file = getattr(cfg, "rust_module_to_file", None) if cfg else None
        crate_to_files = getattr(cfg, "rust_crate_to_files", None) if cfg else None

        for fe in fe_list:
            src_file = to_posix(str(getattr(fe, "id", "") or "").strip())
            src = _map_node_id(src_file)
            if not src:
                continue

            # imports_all is expected to already be in a normalized spec format
            for raw in (getattr(fe, "imports_all", None) or ()):
                spec = str(raw or "").strip()
                if not spec:
                    continue

                if module_to_file and crate_to_files:
                    results = resolve_rust_use(spec, module_to_file=module_to_file, crate_to_files=crate_to_files)
                    for rr in results:
                        if getattr(rr, "kind", "") == "internal":
                            continue
                        kind = f"rust:use:{getattr(rr, 'kind', 'external')}"
                        dst_spec = str(getattr(rr, "spec", spec) or spec).strip()
                        dst = f"ext::{dst_spec}" if dst_spec else f"ext::{spec}"
                        _add_edge(src, dst, kind, 0.4)
                else:
                    # last-resort: keep raw spec, but namespace it to avoid collisions with module node ids
                    _add_edge(src, f"ext::{spec}", "rust:use:external_raw", 0.2)

    # -------------------------
    # Optional enrichment edges (from parse cache)
    # -------------------------
    enrich = bool(getattr(cfg, "rust_edges_enrichment", False)) if cfg else False
    include_mod_edges = bool(getattr(cfg, "rust_edges_mod_decls", False)) if cfg else False
    include_attr_edges = bool(getattr(cfg, "rust_edges_attributes", False)) if cfg else False

    parse_cache = getattr(cfg, "rust_parse_cache", None) if cfg else None

    if enrich and isinstance(parse_cache, dict) and parse_cache:
        def _cache_get(fe: FileEntry) -> Optional[object]:
            # Canonical key first: FileEntry.id (POSIX)
            fe_id = to_posix(str(getattr(fe, "id", "") or "").strip())
            if fe_id and fe_id in parse_cache:
                return parse_cache[fe_id]

            # fallbacks
            rel_file = str(getattr(fe, "file", "") or "").strip()
            relp = to_posix(rel_file)
            for k in (rel_file, relp):
                if k and k in parse_cache:
                    return parse_cache[k]
            try:
                repo_root = getattr(cfg, "repo_root", None)
                if repo_root and rel_file:
                    abs_key = str((Path(repo_root) / rel_file).resolve(strict=False))
                    if abs_key in parse_cache:
                        return parse_cache[abs_key]
            except Exception:
                pass
            return None

        for fe in fe_list:
            src_file = to_posix(str(getattr(fe, "id", "") or "").strip())
            src = _map_node_id(src_file)
            if not src:
                continue

            rec = _cache_get(fe)
            if rec is None or not _is_like_parsed_file(rec):
                continue

            # impl Trait for Type -> edge to trait
            try:
                for impl in (getattr(rec, "impls", None) or ()):
                    trait_name = getattr(impl, "trait_name", None)
                    dst0 = str(trait_name or "").strip()
                    if dst0:
                        _add_edge(src, f"sym::{dst0}", "rust:impl:trait", 0.6)
            except Exception:
                pass

            # trait supertraits -> edge to bound
            try:
                for tr in (getattr(rec, "traits", None) or ()):
                    for st in (getattr(tr, "supertraits", None) or ()):
                        dst0 = str(st or "").strip()
                        if dst0:
                            _add_edge(src, f"sym::{dst0}", "rust:trait:supertrait", 0.6)
            except Exception:
                pass

            # derives -> edge to macro
            try:
                for st in (getattr(rec, "structs", None) or ()):
                    for d in (getattr(st, "derives", None) or ()):
                        dst0 = str(d or "").strip()
                        if dst0:
                            _add_edge(src, f"derive::{dst0}", "rust:derive", 0.5)
            except Exception:
                pass
            try:
                for en in (getattr(rec, "enums", None) or ()):
                    for d in (getattr(en, "derives", None) or ()):
                        dst0 = str(d or "").strip()
                        if dst0:
                            _add_edge(src, f"derive::{dst0}", "rust:derive", 0.5)
            except Exception:
                pass

            # mod declarations -> file/module declares "mod foo"
            if include_mod_edges:
                try:
                    for md in (getattr(rec, "mod_declarations", None) or ()):
                        name = str(getattr(md, "name", "") or "").strip()
                        if name:
                            _add_edge(src, f"sym::mod::{name}", "rust:mod:declares", 0.4)
                except Exception:
                    pass

            # raw attributes -> opt-in only (namespaced)
            if include_attr_edges:
                try:
                    for a in (getattr(rec, "attributes", None) or ()):
                        aa = str(a or "").strip()
                        if aa:
                            _add_edge(src, f"attr::{aa}", "rust:attribute", 0.2)
                except Exception:
                    pass

    # Deterministic edge order
    edges_out.sort(key=lambda e: (str(e.get("kind", "")), str(e.get("src", "")), str(e.get("dst", ""))))

    out = {
        "schema_version": "edges@v1",
        "language": "rust",
        "edges": edges_out,
        "meta": {
            "total": len(edges_out),
            "internal": sum(1 for e in edges_out if e.get("kind") == internal_kind),
            "rust_node_id_space": node_id_space,
            "internal_kind": internal_kind,
            "external_included": bool(include_external),
            "enrichment_included": bool(enrich and isinstance(parse_cache, dict) and parse_cache),
            "mod_decl_edges": bool(enrich and include_mod_edges),
            "attribute_edges": bool(enrich and include_attr_edges),
        },
    }
    return out