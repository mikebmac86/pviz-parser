from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Tuple, Iterable


@dataclass
class ParsedSymbol:
    name: str


@dataclass
class ParsedFileView:
    # --- NodeFacts-compat surface ---
    classes: List[ParsedSymbol] = field(default_factory=list)
    functions: List[ParsedSymbol] = field(default_factory=list)
    globals: List[ParsedSymbol] = field(default_factory=list)
    all_exports: List[str] = field(default_factory=list)
    loc_code: Optional[int] = None
    crosstalk_candidates_py_v1: Tuple[dict, ...] = ()

    # --- Rich Rust surface ---
    module_path: Optional[str] = None
    use_statements: List[Any] = field(default_factory=list)
    mod_declarations: List[Any] = field(default_factory=list)

    structs: List[Any] = field(default_factory=list)
    enums: List[Any] = field(default_factory=list)
    traits: List[Any] = field(default_factory=list)
    impls: List[Any] = field(default_factory=list)

    struct_names: List[str] = field(default_factory=list)
    enum_names: List[str] = field(default_factory=list)
    trait_names: List[str] = field(default_factory=list)
    function_names: List[str] = field(default_factory=list)

    is_lib: bool = False
    is_main: bool = False
    is_mod: bool = False
    has_macro_use: bool = False

    # optional diagnostics (harmless for NodeFacts)
    parse_status: str = "ok"
    problems: List[str] = field(default_factory=list)
    error: Optional[str] = None


def _stable_unique_str(items: Iterable[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for x in items:
        s = (x or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    out.sort()
    return out


def _is_pub_obj(x: Any) -> bool:
    # supports legacy is_pub bool or visibility-ish fields
    try:
        v = getattr(x, "is_pub", None)
        if isinstance(v, bool):
            return v
    except Exception:
        pass
    for attr in ("visibility", "vis"):
        try:
            s = getattr(x, attr, None)
            if isinstance(s, str) and s.strip() and s.strip() != "private":
                return True
        except Exception:
            pass
    return False


def parse_rust_any_file(path: Path, cfg: Any = None) -> Tuple[Optional[ParsedFileView], List[str]]:
    p = Path(path)
    if p.suffix.lower() != ".rs":
        return None, [f"rust_dispatch_unsupported_suffix:{p.suffix.lower()}"]
    return _parse_rust(p, cfg)


def _parse_rust(path: Path, cfg: Any = None) -> Tuple[Optional[ParsedFileView], List[str]]:
    warns: List[str] = []

    # Import: fix module naming and don’t swallow NameError/syntax mistakes
    try:
        from analyzer.rust.rust_parse import parse_rust_file
    except Exception as e:
        return None, [f"rust_parse_error:import:{type(e).__name__}:{str(e)[:200]}"]

    try:
        cli_path = getattr(cfg, "rustparser_cli_path", None) if cfg else None
        cli_path = Path(cli_path) if isinstance(cli_path, str) and cli_path.strip() else cli_path

        rp = parse_rust_file(path, cli_path=cli_path)

        # Build a view even if not ok, for partial data + stable behavior
        parsed = ParsedFileView(
            parse_status=str(getattr(rp, "parse_status", "ok") or "ok"),
            error=getattr(rp, "error", None),
            problems=list(getattr(rp, "problems", None) or []),
            loc_code=getattr(rp, "loc_code", None),
        )

        # If parser says not ok, warn but still return the view
        if not getattr(rp, "ok", False):
            err = getattr(rp, "error", None) or "unknown"
            warns.append(f"rust_parse_not_ok:{err}")

        # Rich fields
        parsed.module_path = getattr(rp, "module_path", None)
        parsed.use_statements = getattr(rp, "use_statements", None) or []
        parsed.mod_declarations = getattr(rp, "mod_declarations", None) or []

        parsed.structs = getattr(rp, "structs", None) or []
        parsed.enums = getattr(rp, "enums", None) or []
        parsed.traits = getattr(rp, "traits", None) or []
        parsed.impls = getattr(rp, "impls", None) or []

        parsed.is_lib = bool(getattr(rp, "is_lib", False))
        parsed.is_main = bool(getattr(rp, "is_main", False))
        parsed.is_mod = bool(getattr(rp, "is_mod", False))
        parsed.has_macro_use = bool(getattr(rp, "has_macro_use", False))

        function_objs = getattr(rp, "functions", None) or []

        # Names (support both objects with .name and plain strings defensively)
        def _names(objs: List[Any]) -> List[str]:
            out: List[str] = []
            for o in objs:
                if isinstance(o, str):
                    out.append(o)
                else:
                    n = getattr(o, "name", None)
                    if isinstance(n, str):
                        out.append(n)
            return out

        struct_names = _names(parsed.structs)
        enum_names = _names(parsed.enums)
        trait_names = _names(parsed.traits)
        function_names = _names(function_objs)

        parsed.struct_names = _stable_unique_str(struct_names)
        parsed.enum_names = _stable_unique_str(enum_names)
        parsed.trait_names = _stable_unique_str(trait_names)
        parsed.function_names = _stable_unique_str(function_names)

        # NodeFacts-facing:
        class_names = parsed.struct_names + parsed.enum_names + parsed.trait_names
        parsed.classes = [ParsedSymbol(n) for n in _stable_unique_str(class_names)]
        parsed.functions = [ParsedSymbol(n) for n in parsed.function_names]

        # globals: support both legacy .fields (List[str]) and rich .fields_rich
        global_names: List[str] = []
        for s in parsed.structs:
            # legacy
            f1 = getattr(s, "fields", None)
            if isinstance(f1, list):
                for x in f1:
                    if isinstance(x, str) and x.strip():
                        global_names.append(x.strip())

            # rich
            f2 = getattr(s, "fields_rich", None)
            if isinstance(f2, list):
                for rf in f2:
                    nm = getattr(rf, "name", None)
                    if isinstance(nm, str) and nm.strip():
                        global_names.append(nm.strip())

        parsed.globals = [ParsedSymbol(n) for n in _stable_unique_str(global_names)]

        # exports: pub items + pub use re-exports
        exports: List[str] = []

        for s in parsed.structs:
            if _is_pub_obj(s):
                n = getattr(s, "name", None)
                if isinstance(n, str):
                    exports.append(n)

        for e in parsed.enums:
            if _is_pub_obj(e):
                n = getattr(e, "name", None)
                if isinstance(n, str):
                    exports.append(n)

        for t in parsed.traits:
            if _is_pub_obj(t):
                n = getattr(t, "name", None)
                if isinstance(n, str):
                    exports.append(n)

        for f in function_objs:
            if _is_pub_obj(f):
                n = getattr(f, "name", None)
                if isinstance(n, str):
                    exports.append(n)

        # pub use (critical for Rust API surface)
        for u in parsed.use_statements:
            if _is_pub_obj(u) or bool(getattr(u, "is_pub", False)):
                pth = getattr(u, "path", None)
                if isinstance(pth, str) and pth.strip():
                    exports.append(pth.strip())

        parsed.all_exports = _stable_unique_str(exports)

        return parsed, warns

    except Exception as e:
        return None, [f"rust_parse_error:{type(e).__name__}:{str(e)[:200]}"]
