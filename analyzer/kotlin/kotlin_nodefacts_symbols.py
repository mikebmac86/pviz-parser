from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Tuple, Union

from analyzer.kotlin.kotlin_canonical import fq_decl
from analyzer.kotlin.parse_kotlin import KotlinParsedFile, parse_kotlin_file


@dataclass(frozen=True)
class NodeFactsSymbols:
    package: Optional[str]
    declared_types: Tuple[str, ...]
    declared_types_fq: Tuple[str, ...]
    public_exports: Tuple[str, ...]
    classes: Tuple[str, ...]
    interfaces: Tuple[str, ...]
    objects: Tuple[str, ...]
    enums: Tuple[str, ...]
    type_aliases: Tuple[str, ...]
    functions: Tuple[str, ...]
    globals: Tuple[str, ...]
    exports: Tuple[str, ...]
    annotations: Tuple[str, ...]
    imports_all_raw: Tuple[str, ...]       # all import paths before resolution
    imports_external: Tuple[str, ...]      # non-internal imports (stdlib + third-party)
    loc_code: Optional[int]
    crosstalk_candidates_py_v1: Tuple[Mapping[str, Any], ...]
    parse_status: str


def _stable(vals: Iterable[str]) -> Tuple[str, ...]:
    return tuple(sorted({str(v).strip() for v in vals if str(v).strip()}))


def _extract_symbols_from_parsed(pf: KotlinParsedFile) -> NodeFactsSymbols:
    pkg = pf.package_name
    annotations: list[str] = list(pf.annotations or [])

    # --- declared types: classes, interfaces, objects, enums, type_aliases ---
    classes_simple: list[str] = []
    interfaces_simple: list[str] = []
    objects_simple: list[str] = []
    enums_simple: list[str] = []
    type_aliases_simple: list[str] = []

    for seq, bucket in (
        (pf.classes,      classes_simple),
        (pf.interfaces,   interfaces_simple),
        (pf.objects,      objects_simple),
        (pf.enums,        enums_simple),
        (pf.type_aliases, type_aliases_simple),
    ):
        for d in (seq or ()):
            nm = str(getattr(d, "name", "") or "").strip()
            if nm:
                bucket.append(nm)
            # Collect per-declaration annotations
            annotations.extend(
                str(x) for x in (getattr(d, "annotations", None) or ())
                if str(x).strip()
            )

    # All declared names combined (for declared_types / fq derivation)
    all_declared = classes_simple + interfaces_simple + objects_simple + enums_simple + type_aliases_simple

    declared_t = _stable(all_declared)
    declared_fq = _stable(fq_decl(pkg, nm) or nm for nm in declared_t)

    funcs = _stable(getattr(f, "name", "") for f in (pf.functions or []))
    props = _stable(getattr(p, "name", "") for p in (pf.properties or []))
    public_exports = declared_fq or declared_t

    # --- import splits ---
    # imports_all_raw: every import path the extractor saw
    imports_all_raw = _stable(
        getattr(pf, "imports_all_raw", None) or
        (i.path for i in (pf.imports or []))
    )
    # imports_external: populated by the folder_index after resolution;
    # fall back to all imports if the field isn't present yet
    imports_external = _stable(getattr(pf, "imports_external", None) or ())

    status = pf.parse_status or ("ok" if pf.ok else "error")
    if pf.problems and status == "ok":
        status = "warn"

    return NodeFactsSymbols(
        package=pkg,
        declared_types=declared_t,
        declared_types_fq=declared_fq,
        public_exports=public_exports,
        classes=_stable(classes_simple),
        interfaces=_stable(interfaces_simple),
        objects=_stable(objects_simple),
        enums=_stable(enums_simple),
        type_aliases=_stable(type_aliases_simple),
        functions=funcs,
        globals=props,
        exports=public_exports,
        annotations=_stable(annotations),
        imports_all_raw=imports_all_raw,
        imports_external=imports_external,
        loc_code=pf.loc_code,
        crosstalk_candidates_py_v1=(),
        parse_status=status,
    )


def parse_symbols_for_nodefacts(path_or_parsed: Union[Path, KotlinParsedFile], cfg: Any = None) -> NodeFactsSymbols:
    try:
        if isinstance(path_or_parsed, KotlinParsedFile):
            return _extract_symbols_from_parsed(path_or_parsed)
        return _extract_symbols_from_parsed(parse_kotlin_file(Path(path_or_parsed), cfg=cfg))
    except Exception:
        return NodeFactsSymbols(
            package=None, declared_types=(), declared_types_fq=(), public_exports=(),
            classes=(), interfaces=(), objects=(), enums=(), type_aliases=(),
            functions=(), globals=(), exports=(), annotations=(),
            imports_all_raw=(), imports_external=(),
            loc_code=None, crosstalk_candidates_py_v1=(), parse_status="error",
        )