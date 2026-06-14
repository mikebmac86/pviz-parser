from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple, Union

from analyzer.ruby.parse_ruby.models import RubyParsedFile
from analyzer.ruby.ruby_canonical import normalize_ruby_require_spec


@dataclass(frozen=True)
class NodeFactsSymbols:
    # Ruby module / namespace. Closest Ruby analog to a package/module path.
    package: Optional[str]

    # Declarations
    declared_types: Tuple[str, ...]       # simple declaration names
    declared_types_fq: Tuple[str, ...]    # fully-qualified declaration names
    public_exports: Tuple[str, ...]       # fq names exposed by this file

    # Ruby-specific declaration buckets
    classes: Tuple[str, ...]
    modules: Tuple[str, ...]
    constants: Tuple[str, ...]

    # Methods declared without an owner.
    top_level_methods: Tuple[str, ...]

    # Optional richer method surface.
    methods: Tuple[str, ...]
    methods_fq: Tuple[str, ...]

    # Rails role: model / controller / job / mailer / route / unknown / etc.
    rails_role: str

    # Import / require facts
    imports_all_raw: Tuple[str, ...]      # all static require specs before resolution
    imports_external: Tuple[str, ...]     # filled by caller/folder-index after resolution

    # Dynamic require/load/autoload diagnostics
    dynamic_requires_count: int

    # Metrics
    loc_code: Optional[int]
    loc_total: Optional[int]
    comment_lines: Optional[int]
    blank_lines: Optional[int]
    comment_pct: Optional[float]
    size_bytes: Optional[int]

    # "ok" | "warn" | "error"
    parse_status: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stable(vals: Iterable[Any]) -> Tuple[str, ...]:
    out = set()
    for v in vals or ():
        try:
            s = str(v).strip()
        except Exception:
            continue
        if s:
            out.add(s)
    return tuple(sorted(out))


def _safe_str(x: Any) -> Optional[str]:
    try:
        if x is None:
            return None
        s = str(x).strip()
        return s or None
    except Exception:
        return None


def _safe_int(x: Any) -> Optional[int]:
    try:
        if x is None:
            return None
        return int(x)
    except Exception:
        return None


def _safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def _cfg_get(cfg: Any, key: str, default: Any = None) -> Any:
    try:
        if isinstance(cfg, Mapping):
            return cfg.get(key, default)
    except Exception:
        pass

    try:
        return getattr(cfg, key, default)
    except Exception:
        return default


def _norm_parse_status(status: Any, *, ok: Any = True, problems: Any = None) -> str:
    s = str(status or "").strip().lower()

    if not s:
        s = "ok" if bool(ok) else "error"

    if s in {"ok", "success"}:
        out = "ok"
    elif "warn" in s or s in {"partial", "degraded"}:
        out = "warn"
    elif "error" in s or "fail" in s or "exception" in s or s in {"err"}:
        out = "error"
    else:
        out = "warn"

    if out == "ok" and problems:
        return "warn"

    return out


def _is_like_ruby_parsed_file(x: Any) -> bool:
    """
    Structural check instead of isinstance only.

    This makes the helper tolerant if cached parsed records come from a slightly
    different import context or test double.
    """
    if x is None:
        return False

    if isinstance(x, RubyParsedFile):
        return True

    try:
        if hasattr(x, "declarations") and hasattr(x, "methods") and hasattr(x, "requires"):
            return True
    except Exception:
        pass

    return False


def _decl_attr(decl: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(decl, name)
    except Exception:
        pass

    if isinstance(decl, Mapping):
        return decl.get(name, default)

    return default


def _method_attr(meth: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(meth, name)
    except Exception:
        pass

    if isinstance(meth, Mapping):
        return meth.get(name, default)

    return default


def _require_attr(req: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(req, name)
    except Exception:
        pass

    if isinstance(req, Mapping):
        return req.get(name, default)

    return default


def _rails_role(pf: Any) -> str:
    try:
        rails = getattr(pf, "rails", None)
        if rails:
            role = getattr(rails, "role", None)
            if role:
                return str(role)
    except Exception:
        pass

    try:
        rails = getattr(pf, "rails", None)
        if isinstance(rails, Mapping):
            role = rails.get("role")
            if role:
                return str(role)
    except Exception:
        pass

    return "unknown"


def _empty_symbols(*, parse_status: str = "error") -> NodeFactsSymbols:
    return NodeFactsSymbols(
        package=None,
        declared_types=(),
        declared_types_fq=(),
        public_exports=(),
        classes=(),
        modules=(),
        constants=(),
        top_level_methods=(),
        methods=(),
        methods_fq=(),
        rails_role="unknown",
        imports_all_raw=(),
        imports_external=(),
        dynamic_requires_count=0,
        loc_code=None,
        loc_total=None,
        comment_lines=None,
        blank_lines=None,
        comment_pct=None,
        size_bytes=None,
        parse_status=parse_status,
    )


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def _extract_symbols_from_parsed(
    pf: Any,
    *,
    imports_external: Optional[Sequence[str]] = None,
) -> NodeFactsSymbols:
    """
    Convert a RubyParsedFile-like object into stable NodeFactsSymbols.

    This function intentionally does not invoke the external Ruby parser. The
    Ruby run/folder-index pipeline owns parser handoff and cache creation.
    """
    classes: list[str] = []
    modules: list[str] = []
    constants: list[str] = []
    fq_names: list[str] = []

    declarations = getattr(pf, "declarations", None) or ()

    for decl in declarations:
        name = _safe_str(_decl_attr(decl, "name"))
        fq = _safe_str(_decl_attr(decl, "fq_name")) or name

        if not fq:
            continue

        fq_names.append(fq)

        kind = str(_decl_attr(decl, "kind", "") or "").strip().lower()

        if kind == "class":
            if name:
                classes.append(name)
        elif kind == "module":
            if name:
                modules.append(name)
        elif kind == "constant":
            if name:
                constants.append(name)

    # Top-level owner: first top-level class/module declared in the file.
    # Used as the "package" analog for grouping.
    top_owner: Optional[str] = None

    for decl in declarations:
        kind = str(_decl_attr(decl, "kind", "") or "").strip().lower()
        owner = _safe_str(_decl_attr(decl, "owner"))

        if kind in {"class", "module"} and not owner:
            top_owner = _safe_str(_decl_attr(decl, "fq_name")) or _safe_str(_decl_attr(decl, "name"))
            break

    # Method extraction.
    top_level_methods: list[str] = []
    method_names: list[str] = []
    method_fq_names: list[str] = []

    for meth in getattr(pf, "methods", None) or ():
        name = _safe_str(_method_attr(meth, "name"))
        owner = _safe_str(_method_attr(meth, "owner"))
        fq = _safe_str(_method_attr(meth, "fq_name"))

        if name:
            method_names.append(name)

        if fq:
            method_fq_names.append(fq)
        elif owner and name:
            method_fq_names.append(f"{owner}#{name}")

        # Keep top-level method meaning strict: ownerless methods only.
        if name and not owner:
            top_level_methods.append(name)

    declared_types = _stable([*classes, *modules, *constants])
    declared_types_fq = _stable(fq_names)
    public_exports = declared_types_fq if declared_types_fq else declared_types

    imports_all_raw: list[str] = []
    dynamic_requires_count = 0

    for req in getattr(pf, "requires", None) or ():
        spec = _safe_str(_require_attr(req, "spec"))
        dynamic = bool(_require_attr(req, "dynamic", False))

        if dynamic or not spec:
            dynamic_requires_count += 1
            continue

        norm = normalize_ruby_require_spec(spec)
        if norm:
            imports_all_raw.append(norm)

    parse_status = _norm_parse_status(
        getattr(pf, "parse_status", None),
        ok=getattr(pf, "ok", True),
        problems=getattr(pf, "problems", None),
    )

    return NodeFactsSymbols(
        package=top_owner,
        declared_types=declared_types,
        declared_types_fq=declared_types_fq,
        public_exports=public_exports,
        classes=_stable(classes),
        modules=_stable(modules),
        constants=_stable(constants),
        top_level_methods=_stable(top_level_methods),
        methods=_stable(method_names),
        methods_fq=_stable(method_fq_names),
        rails_role=_rails_role(pf),
        imports_all_raw=_stable(imports_all_raw),
        imports_external=_stable(imports_external or ()),
        dynamic_requires_count=dynamic_requires_count,
        loc_code=_safe_int(getattr(pf, "sloc", None)),
        loc_total=_safe_int(getattr(pf, "loc", None)),
        comment_lines=_safe_int(getattr(pf, "comment_lines", None)),
        blank_lines=_safe_int(getattr(pf, "blank_lines", None)),
        comment_pct=_safe_float(getattr(pf, "comment_pct", None)),
        size_bytes=_safe_int(getattr(pf, "size_bytes", None)),
        parse_status=parse_status,
    )


def extract_symbols(
    pf: RubyParsedFile,
    *,
    imports_external: Optional[Sequence[str]] = None,
) -> NodeFactsSymbols:
    """
    Back-compatible public helper.

    Project a RubyParsedFile into flat NodeFactsSymbols for nodefacts output.
    """
    return _extract_symbols_from_parsed(pf, imports_external=imports_external)


_ERROR_SYMBOLS = _empty_symbols(parse_status="error")


def safe_extract_symbols(
    pf: Optional[RubyParsedFile],
    *,
    imports_external: Optional[Sequence[str]] = None,
) -> NodeFactsSymbols:
    """
    Safe wrapper for existing callers.

    Tolerance contract:
      - Never raises.
      - Returns empty error symbols on failure.
    """
    if pf is None:
        return _ERROR_SYMBOLS

    try:
        return extract_symbols(pf, imports_external=imports_external)
    except Exception:
        return _ERROR_SYMBOLS


def parse_symbols_for_nodefacts(
    path_or_parsed: Any,
    cfg: Any = None,
    *,
    imports_external: Optional[Sequence[str]] = None,
) -> NodeFactsSymbols:
    """
    Rust-style nodefacts entrypoint for Ruby.

    Accepts:
      - RubyParsedFile-like object: uses cached parser output.
      - Path/string: intentionally not reparsed; returns error symbols unless
        cfg.ruby_parse_cache contains a matching key.

    This keeps Ruby nodefacts aligned with Rust's cached-object fast path without
    re-invoking the external Ruby parser during nodefacts generation.
    """
    cfg_obj: Any = cfg
    try:
        if isinstance(cfg, Mapping):
            cfg_obj = SimpleNamespace(**dict(cfg))
    except Exception:
        cfg_obj = cfg

    # Fast path: cached parsed object.
    if _is_like_ruby_parsed_file(path_or_parsed):
        try:
            return _extract_symbols_from_parsed(
                path_or_parsed,
                imports_external=imports_external,
            )
        except Exception:
            return _ERROR_SYMBOLS

    # Cache lookup path for callers that pass a file id/path.
    try:
        key = str(path_or_parsed).replace("\\", "/").strip("/")
    except Exception:
        return _ERROR_SYMBOLS

    cache = _cfg_get(cfg_obj, "ruby_parse_cache", None)

    if isinstance(cache, Mapping):
        candidates = [
            key,
            str(Path(key)).replace("\\", "/").strip("/"),
        ]

        for cand in candidates:
            try:
                cached = cache.get(cand)
            except Exception:
                cached = None

            if _is_like_ruby_parsed_file(cached):
                try:
                    return _extract_symbols_from_parsed(
                        cached,
                        imports_external=imports_external,
                    )
                except Exception:
                    return _ERROR_SYMBOLS

    # Ruby nodefacts should not invoke the external parser here.
    return _ERROR_SYMBOLS