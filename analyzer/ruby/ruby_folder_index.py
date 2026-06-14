from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from analyzer.ruby.parse_ruby.models import RubyAnalysis, RubyParsedFile
from analyzer.ruby.ruby_canonical import is_stdlib_or_gem_require, normalize_ruby_require_spec
from analyzer.ruby.ruby_nodefacts_symbols import safe_extract_symbols
from analyzer.ruby.ruby_require_resolver import build_require_edges, build_require_spec_to_files
from analyzer_store.types import FileEntry, FolderIndex

try:
    from analyzer_store.types import FOLDER_INDEX_SCHEMA
except Exception:
    FOLDER_INDEX_SCHEMA = "folder-index-1.1.0"


# ---------------------------------------------------------------------------
# Ruby-specific lookup tables
# ---------------------------------------------------------------------------

@dataclass
class RubyIndexTables:
    """
    Ruby-specific cross-file lookup tables built during folder index construction.

    Kept separate from FolderIndex because they are Ruby-semantic and not part of
    the canonical folder-index schema.
    """
    fq_decl_to_file: Dict[str, str] = field(default_factory=dict)
    module_to_files: Dict[str, List[str]] = field(default_factory=dict)

    # require spec -> files that request the spec
    require_spec_to_files: Dict[str, List[str]] = field(default_factory=dict)

    # require spec -> files that provide the spec
    require_spec_to_provider_files: Dict[str, List[str]] = field(default_factory=dict)

    # file -> normalized static require specs
    file_to_require_specs: Dict[str, List[str]] = field(default_factory=dict)

    # file -> dynamic require/load/autoload records
    dynamic_requires_by_file: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)

    # Per-file Ruby symbol facts keyed by rel_path.
    symbols_by_file: Dict[str, Any] = field(default_factory=dict)

    # Per-file error string if parse failed.
    error_by_file: Dict[str, Optional[str]] = field(default_factory=dict)

    # Per-file Rails role.
    rails_role_by_file: Dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_posix(s: Any) -> str:
    return str(s or "").replace("\\", "/").strip("/")

def _json_safe(obj: Any) -> Any:
    """
    Convert Ruby parser model objects such as RubyLoc into JSON-safe values.

    This is intentionally generic because Ruby parser records may contain nested
    dataclasses/objects in loc fields, declarations, methods, requires, Rails
    records, etc.
    """
    if obj is None:
        return None

    if isinstance(obj, (str, int, float, bool)):
        return obj

    if isinstance(obj, Path):
        return str(obj)

    if is_dataclass(obj):
        try:
            return _json_safe(asdict(obj))
        except Exception:
            pass

    if isinstance(obj, dict):
        return {
            str(_json_safe(k)): _json_safe(v)
            for k, v in obj.items()
        }

    if isinstance(obj, (list, tuple, set)):
        return [_json_safe(v) for v in obj]

    # Handle parser model objects like RubyLoc that are not dataclasses.
    d = getattr(obj, "__dict__", None)
    if isinstance(d, dict):
        return {
            str(k): _json_safe(v)
            for k, v in d.items()
            if not str(k).startswith("_")
        }

    # Last-resort stable fallback.
    return str(obj)

def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None:
            return default
        return int(v)
    except Exception:
        return default


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _dedupe_sorted(values: Sequence[str]) -> List[str]:
    return sorted({str(v) for v in values if str(v).strip()})


def _append_unique(mapping: Dict[str, List[str]], key: str, value: str) -> None:
    if not key or not value:
        return
    bucket = mapping.setdefault(key, [])
    if value not in bucket:
        bucket.append(value)


def _file_name(rel: str) -> str:
    return rel.rsplit("/", 1)[-1] if "/" in rel else rel


def _iter_requires(pf: RubyParsedFile) -> Sequence[Any]:
    try:
        return pf.requires or []
    except Exception:
        return []


def _require_spec(req: Any) -> Optional[str]:
    try:
        spec = getattr(req, "spec", None)
    except Exception:
        spec = None

    if spec is None and isinstance(req, dict):
        spec = req.get("spec")

    if spec is None:
        return None

    norm = normalize_ruby_require_spec(str(spec))
    return norm or None


def _require_dynamic(req: Any) -> bool:
    try:
        if bool(getattr(req, "dynamic", False)):
            return True
    except Exception:
        pass

    if isinstance(req, dict):
        if bool(req.get("dynamic")):
            return True
        if not req.get("spec"):
            return True

    return _require_spec(req) is None


def _require_to_raw(req: Any) -> Dict[str, Any]:
    if isinstance(req, dict):
        return dict(req)

    out: Dict[str, Any] = {}

    for attr in ("spec", "kind", "dynamic", "raw"):
        try:
            val = getattr(req, attr)
            if val is not None:
                out[attr] = val
        except Exception:
            pass

    try:
        loc = getattr(req, "loc", None)
        if loc is not None:
            out["loc"] = _json_safe(loc)
    except Exception:
        pass

    return out or {"raw": str(req)}


def _extract_provider_index(analysis: RubyAnalysis) -> Dict[str, List[str]]:
    """
    Prefer the Ruby extractor's indexes.requires.by_provider map:

      require spec -> provider file(s)

    This is the authoritative source for internal require resolution when present.
    """
    indexes = analysis.indexes or {}
    requires = indexes.get("requires") if isinstance(indexes, dict) else {}
    by_provider = requires.get("by_provider") if isinstance(requires, dict) else {}

    out: Dict[str, List[str]] = {}

    if not isinstance(by_provider, dict):
        return out

    for spec, providers in by_provider.items():
        norm = normalize_ruby_require_spec(str(spec))
        if not norm:
            continue

        vals: List[str] = []

        if isinstance(providers, (list, tuple, set)):
            for p in providers:
                rel = _to_posix(p)
                if rel:
                    vals.append(rel)
        elif isinstance(providers, str):
            rel = _to_posix(providers)
            if rel:
                vals.append(rel)

        if vals:
            out[norm] = _dedupe_sorted(vals)

    return dict(sorted(out.items(), key=lambda kv: kv[0]))


def _extract_declared_constant_index(analysis: RubyAnalysis) -> Dict[str, List[str]]:
    indexes = analysis.indexes or {}
    constants = indexes.get("constants") if isinstance(indexes, dict) else {}
    declared = constants.get("declared") if isinstance(constants, dict) else {}

    out: Dict[str, List[str]] = {}

    if not isinstance(declared, dict):
        return out

    for fq_name, files in declared.items():
        fq = str(fq_name).strip()
        if not fq:
            continue

        vals: List[str] = []
        if isinstance(files, (list, tuple, set)):
            vals = [_to_posix(f) for f in files if _to_posix(f)]
        elif isinstance(files, str):
            vals = [_to_posix(files)]

        if vals:
            out[fq] = _dedupe_sorted(vals)

    return dict(sorted(out.items(), key=lambda kv: kv[0]))


def _is_external_require(spec: str, *, provider_files: Sequence[str]) -> bool:
    """
    Classify external only after internal provider lookup.

    Stdlib/gem specs are external even if a local basename happens to collide.
    Other unresolved specs are also external for nodefacts/import summary purposes.
    """
    if provider_files:
        return False
    return True

def _provider_is_allowed_internal(spec: str, dst: str, src: str) -> bool:
    """
    Decide whether a proven local provider may become an internal require edge.

    Rule:
      - Never create self-edges.
      - Bare stdlib/default-gem names stay external even if a local file happens
        to share the same basename.
      - Path-like specs with a proven local provider are allowed.

    Examples:
      require "logger"                 -> external, even if logger.rb exists
      require "json"                   -> external, even if json.rb exists
      require "rack"                   -> external if treated as bare gem
      require "rack/protection/base"   -> internal when provider exists
      require "sinatra/base"           -> internal when provider exists
      require "spec_helper"            -> internal when ranked to local helper
    """
    spec = normalize_ruby_require_spec(spec)
    dst = _to_posix(dst)
    src = _to_posix(src)

    if not spec or not dst or not src:
        return False

    if src == dst:
        return False

    # Only block bare stdlib/default-gem collisions. Path-like specs with local
    # providers are meaningful project structure and should resolve internally.
    if "/" not in spec and is_stdlib_or_gem_require(spec):
        return False

    return True

def _ruby_package_root(rel: str) -> str:
    """
    Best-effort Ruby package/gem root for source-aware require resolution.

    This is intentionally heuristic. It is used only to rank ambiguous provider
    candidates such as multiple spec_helper.rb files in a monorepo-style repo.
    """
    rel = _to_posix(rel)
    parts = rel.split("/")

    if not parts:
        return ""

    # Common Ruby repo layouts:
    #   lib/...
    #   test/...
    #   spec/...
    #   rack-protection/lib/...
    #   sinatra-contrib/spec/...
    for marker in ("lib", "spec", "test", "features", "app", "config"):
        if marker in parts:
            idx = parts.index(marker)
            if idx == 0:
                return ""
            return "/".join(parts[:idx])

    # Fall back to first path segment for nested repos, or repo root for flat files.
    return parts[0] if len(parts) > 1 else ""


def _common_prefix_len(a: Sequence[str], b: Sequence[str]) -> int:
    n = 0
    for left, right in zip(a, b):
        if left != right:
            break
        n += 1
    return n


def _rank_provider_candidates(src: str, spec: str, candidates: Sequence[str]) -> List[str]:
    """
    Pick the best local provider(s) for a require spec.

    For most requires, a single provider is expected. For ambiguous helper-like
    specs such as "spec_helper" or "test_helper", prefer the provider in the
    same package/gem root and then nearest path prefix.

    This avoids cases where:
      sinatra-contrib/spec/foo_spec.rb
        require "spec_helper"

    resolves to both:
      rack-protection/spec/spec_helper.rb
      sinatra-contrib/spec/spec_helper.rb
    """
    src = _to_posix(src)
    spec = normalize_ruby_require_spec(spec)
    unique = _dedupe_sorted([_to_posix(c) for c in candidates if _to_posix(c)])

    if len(unique) <= 1:
        return unique

    src_root = _ruby_package_root(src)

    same_root = [
        c for c in unique
        if _ruby_package_root(c) == src_root
    ]

    ranked_pool = same_root or unique

    ranked = sorted(
        ranked_pool,
        key=lambda c: (
            _common_prefix_len(src.split("/"), c.split("/")),
            -abs(len(src.split("/")) - len(c.split("/"))),
            c,
        ),
        reverse=True,
    )

    # Ruby require resolves to the first matching load path entry. In the bundle,
    # a single best provider is usually more useful than emitting cross-package
    # helper edges. Keep this intentionally conservative.
    return ranked[:1]

# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def build_folder_index(
    *,
    analysis: RubyAnalysis,
    cfg: Any = None,
) -> Tuple[FolderIndex, RubyIndexTables, Dict[str, RubyParsedFile]]:
    """
    Build a canonical FolderIndex from a completed RubyAnalysis.

    Returns:
      - FolderIndex: canonical artifact consumed by build_nodefacts_from_folder_index
      - RubyIndexTables: Ruby-specific lookup tables for require resolution,
        declaration indexing, and symbol facts
      - parse_cache: rel_path -> RubyParsedFile for downstream consumers

    Pipeline mirrors other language analyzers:
      Phase 1: per-file symbol extraction + declaration lookup tables
      Phase 2: require resolution -> imports_internal / imports_all per file
      Phase 3: assemble FileEntry objects and FolderIndex
      Phase 4: aggregate FolderIndex metadata
    """
    t0 = time.perf_counter()

    files_in = analysis.files or {}
    tables = RubyIndexTables()
    parse_cache: Dict[str, RubyParsedFile] = {}

    provider_index = _extract_provider_index(analysis)
    declared_constant_index = _extract_declared_constant_index(analysis)

    # ------------------------------------------------------------------
    # Phase 1: Per-file symbol extraction + lookup table construction
    # ------------------------------------------------------------------
    _file_data: Dict[str, Dict[str, Any]] = {}

    for rel_raw, pf in files_in.items():
        rel = _to_posix(rel_raw)
        parse_cache[rel] = pf

        syms = safe_extract_symbols(pf)

        tables.symbols_by_file[rel] = syms
        tables.error_by_file[rel] = getattr(pf, "error", None)
        tables.rails_role_by_file[rel] = getattr(syms, "rails_role", "unknown")

        # Populate fq_decl_to_file from symbol extraction.
        for fq in getattr(syms, "declared_types_fq", []) or []:
            if fq and fq not in tables.fq_decl_to_file:
                tables.fq_decl_to_file[str(fq)] = rel

        # Populate module_to_files.
        for mod in getattr(syms, "modules", []) or []:
            _append_unique(tables.module_to_files, str(mod), rel)

        _file_data[rel] = {
            "pf": pf,
            "syms": syms,
            "parse_status": getattr(syms, "parse_status", None) or getattr(pf, "parse_status", "ok"),
        }

    # Propagate Ruby-side constant index entries.
    # Keep first file as canonical owner, but do not discard provider lists;
    # repeated/reopened modules are normal Ruby behavior.
    for fq_name, file_list in declared_constant_index.items():
        if file_list:
            tables.fq_decl_to_file.setdefault(str(fq_name), file_list[0])
            root_mod = str(fq_name).split("::", 1)[0]
            for rel in file_list:
                _append_unique(tables.module_to_files, root_mod, rel)

    # ------------------------------------------------------------------
    # Phase 2: Require indexes and resolution
    # ------------------------------------------------------------------
    indexes = analysis.indexes or {}
    require_index = indexes.get("requires") if isinstance(indexes, dict) else {}

    # Existing helper remains useful for requester index:
    # require spec -> files that request it.
    tables.require_spec_to_files = build_require_spec_to_files(require_index or {})

    # Provider index from extractor:
    # require spec -> files that provide it.
    tables.require_spec_to_provider_files = provider_index

    # Existing edge helper can still contribute fallback edges if it knows more
    # than the provider index. Provider-index resolution below remains primary.
    fallback_require_edges = build_require_edges(
        parsed_files=parse_cache,
        require_spec_to_files=tables.require_spec_to_files,
        fq_decl_to_file=tables.fq_decl_to_file,
    )

    internal_by_file: Dict[str, List[str]] = {rel: [] for rel in _file_data}
    external_by_file: Dict[str, List[str]] = {rel: [] for rel in _file_data}
    imports_all_by_file: Dict[str, List[str]] = {rel: [] for rel in _file_data}

    # Primary provider-index resolution.
    for rel, data in _file_data.items():
        pf: RubyParsedFile = data["pf"]

        for req in _iter_requires(pf):
            if _require_dynamic(req):
                tables.dynamic_requires_by_file.setdefault(rel, []).append(_json_safe(_require_to_raw(req)))
                continue

            spec = _require_spec(req)
            if not spec:
                continue

            _append_unique(imports_all_by_file, rel, spec)
            _append_unique(tables.file_to_require_specs, rel, spec)

            raw_providers = provider_index.get(spec, []) or []
            ranked_providers = _rank_provider_candidates(rel, spec, raw_providers)

            allowed_providers = [
                dst
                for dst in ranked_providers
                if _provider_is_allowed_internal(spec, dst, rel)
            ]

            if allowed_providers:
                for dst in allowed_providers:
                    _append_unique(internal_by_file, rel, dst)
            elif _is_external_require(spec, provider_files=allowed_providers):
                _append_unique(external_by_file, rel, spec)            

    # Fallback edges from existing resolver.
    # This protects any special resolver logic already implemented, while still
    # avoiding stdlib/gem collisions.
    for edge in fallback_require_edges:
        src = _to_posix(getattr(edge, "source", ""))
        dst = _to_posix(getattr(edge, "target", ""))
        spec = normalize_ruby_require_spec(str(getattr(edge, "spec", "") or ""))

        if not src or not dst:
            continue

        if spec and not _provider_is_allowed_internal(spec, dst, src):
            continue

        # Do not let fallback edges override a provider-index decision that already
        # classified the spec as external or resolved it to a better ranked provider.
        if spec:
            existing_for_spec = provider_index.get(spec, []) or []
            if existing_for_spec:
                ranked = _rank_provider_candidates(src, spec, existing_for_spec)
                if dst not in ranked:
                    continue

        _append_unique(internal_by_file, src, dst)

    # Ensure deterministic table values.
    for k in list(tables.require_spec_to_files.keys()):
        tables.require_spec_to_files[k] = _dedupe_sorted(tables.require_spec_to_files[k])

    for k in list(tables.require_spec_to_provider_files.keys()):
        tables.require_spec_to_provider_files[k] = _dedupe_sorted(tables.require_spec_to_provider_files[k])

    for k in list(tables.file_to_require_specs.keys()):
        tables.file_to_require_specs[k] = _dedupe_sorted(tables.file_to_require_specs[k])

    for k in list(tables.module_to_files.keys()):
        tables.module_to_files[k] = _dedupe_sorted(tables.module_to_files[k])

    # ------------------------------------------------------------------
    # Phase 3: Assemble FileEntry objects
    # ------------------------------------------------------------------
    file_entries: Dict[str, FileEntry] = {}

    for rel, data in _file_data.items():
        pf: RubyParsedFile = data["pf"]
        syms = data["syms"]

        imports_internal = tuple(sorted(set(internal_by_file.get(rel, []))))
        imports_all = tuple(sorted(set(imports_all_by_file.get(rel, []) or getattr(syms, "imports_all_raw", []) or [])))
        imports_external = tuple(sorted(set(external_by_file.get(rel, []))))

        parse_status = str(data["parse_status"] or "ok")
        error = getattr(pf, "error", None)

        # Per-file Rails facts (role, path_convention, DSL: associations,
        # callbacks, validations, scopes, routes, async_invocations).
        # Sourced from RubyParsedFile.rails, mirroring ruby_rails_index.py's
        # build_rails_annotations(), which reads pf.rails the same way.
        rails_obj = getattr(pf, "rails", None)
        rails_facts: Optional[Dict[str, Any]] = None
        if rails_obj is not None:
            rails_dsl = getattr(rails_obj, "dsl", None) or {}
            rails_facts = {
                "role": getattr(rails_obj, "role", None) or tables.rails_role_by_file.get(rel, "unknown"),
                "path_convention": getattr(rails_obj, "path_convention", None),
                "dsl": _json_safe(rails_dsl),
            }

        ruby_language_facts: Dict[str, Any] = {
            "requires": [
                _json_safe(_require_to_raw(req))
                for req in _iter_requires(pf)
                if not _require_dynamic(req)
            ],
            "dynamic_requires": list(tables.dynamic_requires_by_file.get(rel, [])),
            "symbols": _json_safe({
                "declared_types": list(getattr(syms, "declared_types", []) or []),
                "declared_types_fq": list(getattr(syms, "declared_types_fq", []) or []),
                "classes": list(getattr(syms, "classes", []) or []),
                "modules": list(getattr(syms, "modules", []) or []),
                "constants": list(getattr(syms, "constants", []) or []),
                "top_level_methods": list(getattr(syms, "top_level_methods", []) or []),
                "imports_all_raw": list(getattr(syms, "imports_all_raw", []) or []),
                "package": getattr(syms, "package", None),
                "rails_role": tables.rails_role_by_file.get(rel, "unknown"),
            }),
            "rails": rails_facts,
        }

        entry = FileEntry(
            id=rel,
            file=rel,
            name=_file_name(rel),
            parse_status=parse_status,
            imports_all=imports_all,
            imports_internal=imports_internal,
            imports_runtime=imports_internal,
            imports_runtime_internal=imports_internal,
            loc=_safe_int(getattr(syms, "loc_total", 0)) or None,
            sloc=_safe_int(getattr(syms, "loc_code", 0)) or None,
            comment_lines=_safe_int(getattr(syms, "comment_lines", 0)) or None,
            blank_lines=_safe_int(getattr(syms, "blank_lines", 0)) or None,
            comment_pct=_safe_float(getattr(syms, "comment_pct", None)),
            size_bytes=_safe_int(getattr(syms, "size_bytes", 0)) or None,
            mtime=None,
            hash=None,
            import_style_counts={},
            symbol_internal=tuple(getattr(syms, "declared_types_fq", []) or ()),
            imports_external=imports_external,
            language_facts={
                "ruby": _json_safe(ruby_language_facts),
            },
            error_snippet=error,
            eligible=True,
        )

        file_entries[rel] = entry

    # ------------------------------------------------------------------
    # Phase 4: Metadata
    # ------------------------------------------------------------------
    parsed_ok = sum(1 for e in file_entries.values() if e.parse_status == "ok")
    parse_issues = sum(1 for e in file_entries.values() if e.parse_status != "ok")
    internal_edge_count = sum(len(e.imports_internal or ()) for e in file_entries.values())
    external_import_count = sum(len(e.imports_external or ()) for e in file_entries.values())

    total_loc = sum(_safe_int(e.loc) for e in file_entries.values())
    total_sloc = sum(_safe_int(e.sloc) for e in file_entries.values())
    total_comment_lines = sum(_safe_int(getattr(e, "comment_lines", None)) for e in file_entries.values())
    total_blank_lines = sum(_safe_int(getattr(e, "blank_lines", None)) for e in file_entries.values())
    comment_pct_total = (total_comment_lines / total_loc) if total_loc else None

    dynamic_require_count = sum(len(v) for v in tables.dynamic_requires_by_file.values())

    analysis_meta = dict(analysis.meta or {})

    meta: Dict[str, str] = {
        "created": "",
        "root": str(analysis_meta.get("repo_root", "")),
        "language": "ruby",
        "rails_detected": str(analysis_meta.get("rails_detected", False)).lower(),
        "parser": str(analysis_meta.get("parser", "pviz-ruby-extract")),

        "external_import_count": str(external_import_count),
        "eligible_count": str(len(file_entries)),
        "parsed_count": str(parsed_ok),
        "parse_issues_count": str(parse_issues),
        "internal_edge_count": str(internal_edge_count),

        # Critical for final bundle metrics.
        "total_loc": str(int(total_loc)),
        "total_sloc": str(int(total_sloc)),
        "total_comment_lines": str(int(total_comment_lines)),
        "total_blank_lines": str(int(total_blank_lines)),
        "comment_pct": str(comment_pct_total if comment_pct_total is not None else ""),

        "fq_decl_index_count": str(len(tables.fq_decl_to_file)),
        "module_index_count": str(len(tables.module_to_files)),
        "require_spec_index_count": str(len(tables.require_spec_to_files)),
        "require_provider_index_count": str(len(tables.require_spec_to_provider_files)),
        "dynamic_require_file_count": str(len(tables.dynamic_requires_by_file)),
        "dynamic_require_count": str(dynamic_require_count),

        "build_ms": str(int((time.perf_counter() - t0) * 1000)),
    }

    idx = FolderIndex(
        schema=FOLDER_INDEX_SCHEMA,
        meta=meta,
        files=dict(sorted(file_entries.items(), key=lambda kv: kv[0])),
    )

    return idx, tables, dict(sorted(parse_cache.items(), key=lambda kv: kv[0]))


def save_folder_index(
    idx: FolderIndex,
    tables: RubyIndexTables,
    path: Path,
) -> None:
    """
    Serialize FolderIndex + RubyIndexTables to JSON at path.

    Uses canonical FolderIndex-style file records with folder_id, while also
    preserving Ruby-specific tables and symbol facts.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    files_out: Dict[str, Any] = {}

    for rel, entry in idx.files.items():
        rec = asdict(entry)

        if "id" in rec:
            rec["folder_id"] = rec.pop("id")

        syms = tables.symbols_by_file.get(rel)

        rec["ok"] = entry.parse_status == "ok"
        rec["rails_role"] = tables.rails_role_by_file.get(rel, "unknown")
        rec["error"] = tables.error_by_file.get(rel)

        if syms is not None:
            rec.update({
                "declared_types": list(getattr(syms, "declared_types", []) or []),
                "declared_types_fq": list(getattr(syms, "declared_types_fq", []) or []),
                "classes": list(getattr(syms, "classes", []) or []),
                "modules": list(getattr(syms, "modules", []) or []),
                "constants": list(getattr(syms, "constants", []) or []),
                "top_level_methods": list(getattr(syms, "top_level_methods", []) or []),
                "imports_all_raw": list(getattr(syms, "imports_all_raw", []) or []),
                "package": getattr(syms, "package", None),
            })

        files_out[rel] = {k: v for k, v in rec.items() if v is not None}

    obj = {
        "schema": idx.schema,
        "meta": dict(idx.meta),
        "files": files_out,
        "ruby_tables": {
            "fq_decl_to_file": dict(sorted(tables.fq_decl_to_file.items())),
            "module_to_files": {
                k: _dedupe_sorted(v)
                for k, v in sorted(tables.module_to_files.items())
            },
            "require_spec_to_files": {
                k: _dedupe_sorted(v)
                for k, v in sorted(tables.require_spec_to_files.items())
            },
            "require_spec_to_provider_files": {
                k: _dedupe_sorted(v)
                for k, v in sorted(tables.require_spec_to_provider_files.items())
            },
            "file_to_require_specs": {
                k: _dedupe_sorted(v)
                for k, v in sorted(tables.file_to_require_specs.items())
            },
            "dynamic_requires_by_file": {
                k: list(v)
                for k, v in sorted(tables.dynamic_requires_by_file.items())
            },
        },
    }

    tmp = str(path) + f".tmp.{id(obj)}"
    try:
        Path(tmp).write_text(
            json.dumps(_json_safe(obj), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        Path(tmp).replace(path)
    except Exception:
        try:
            Path(tmp).unlink(missing_ok=True)
        except Exception:
            pass
        raise